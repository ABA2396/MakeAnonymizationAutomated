# -*- coding: utf-8 -*-
"""
截图匿名化核心库（半自动流程后端，供 ui.py / mcp_server.py / 命令行调用）

标注模型（<图目录>/output/marks/<stem>.json）:
    {"marks": [{"avatar": [cx, cy, r] | null,
                "name_box": [x1, y1, x2, y2] | null,
                "name": "用户名" | null,
                "draft": true|false}]}
- avatar: 人工点击吸附得到的头像圆，导出时画纯色字母圆
- name_box: 人工框选的文本覆盖框，导出时涂背景色并按用户专属颜色写 ｢用户首字母｣
  （与头像圆同色，同网页版；name=null 时纯涂色，用于不想进映射的一次性遮挡）
- name 为真实用户名，经全局映射（output/mapping.json）保证跨图同色同字母
- draft=true 的标注只作预填草稿显示，导出时忽略
- 导出时自动扫描（可用 auto_scan=False 关闭）：OCR 全图后对 @已知用户名 与
  灰字引用标题行打码；名字全部经人工/标注确认，误报率远低于全自动检测
"""
import os
import re
import json
import colorsys
import hashlib
import threading
import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont
from pypinyin import lazy_pinyin

OFFICIAL = "MAA-Official"

RE_TIME = re.compile(
    r"^\s*(20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}"
    r"|刚刚|半小时前|昨天|前天|今天|\d+\s*(?:秒|分钟|小时|天)前)"
)
# LV 徽章 OCR 变体：LU4/LUG/LuG4/LUE/LUH/LIVE/LU5⚡ 等
RE_BADGE = re.compile(r"^(?:[LUlu][Uu][0-9A-Z⚡️]{0,3}|[LUlu][Uu]?[GEge][0-9]{0,2}|LIVE)$")
OFFICIAL_PREFIX = re.compile(r"^MAA[-_]?Official", re.I)
BLACKLIST_EXACT = {"回复", "赞", "已赞", "点踩", "翻译", "举报", "置顶", "作者", "楼主"}
RE_PURE_NUM = re.compile(r"^[凸]{0,2}\d+$")
RE_PUNCT = re.compile(r"^[。，、．.!！?？：:；;~～…—《》<>()（）\[\]{}\"'|/\\*_#%&@$^=+]+$")

_font_cache = {}


def is_blacklisted(text):
    t = text.strip()
    return t in BLACKLIST_EXACT or bool(RE_PURE_NUM.fullmatch(t)) or bool(RE_PUNCT.fullmatch(t))


def get_font(size, bold=False):
    key = (int(size), bold)
    if key not in _font_cache:
        path = r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"
        _font_cache[key] = ImageFont.truetype(path, int(size))
    return _font_cache[key]


def imread_u(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite_u(path, img):
    ok, buf = cv2.imencode(os.path.splitext(path)[1], img)
    buf.tofile(path)
    return ok


# ---------------- 目录布局 ----------------

class Workspace:
    """一个图片目录对应的输出布局与全局映射"""

    def __init__(self, src_dir, restart=False):
        self.src_dir = os.path.abspath(src_dir)
        self.out_dir = os.path.join(self.src_dir, "output")
        self.cmp_dir = os.path.join(self.out_dir, "compare")
        self.marks_dir = os.path.join(self.out_dir, "marks")
        self.mapping_path = os.path.join(self.out_dir, "mapping.json")
        self.cache_dir = os.path.join(self.out_dir, ".ocr_cache")
        if restart and os.path.isdir(self.out_dir):
            import shutil
            shutil.rmtree(self.out_dir)
        for d in (self.out_dir, self.cmp_dir, self.marks_dir, self.cache_dir):
            os.makedirs(d, exist_ok=True)
        self.mapping = self.load_mapping()

    def load_mapping(self):
        if os.path.exists(self.mapping_path):
            with open(self.mapping_path, encoding="utf-8") as f:
                return json.load(f)
        return {"users": {}}

    def save_mapping(self):
        with open(self.mapping_path, "w", encoding="utf-8") as f:
            json.dump(self.mapping, f, ensure_ascii=False, indent=1)

    def list_images(self):
        files = [f for f in os.listdir(self.src_dir) if f.lower().endswith(".png")]
        return sorted(files, key=natkey)

    def image_path(self, stem):
        return os.path.join(self.src_dir, stem + ".png")

    def marks_path(self, stem):
        return os.path.join(self.marks_dir, stem + ".json")

    def load_marks(self, stem):
        p = self.marks_path(stem)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f).get("marks", [])
        return []

    def save_marks(self, stem, marks):
        p = self.marks_path(stem)
        if os.path.exists(p):
            # 上一版留 .bak，人工改崩/误保存时可手动改回
            try:
                import shutil
                shutil.copyfile(p, p + ".bak")
            except OSError:
                pass
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"marks": marks}, f, ensure_ascii=False, indent=1)


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# ---------------- OCR（大图切块；结果落盘缓存） ----------------

def ocr_factory():
    # torch 须先于 paddle 进程：两者 cudnn 同为 9.5.1 可复用，paddle 先占坑会弄坏 torch 的 DLL
    try:
        import torch  # noqa: F401
    except ImportError:
        pass
    from paddleocr import PaddleOCR
    device = os.environ.get("ANON_OCR_DEVICE", "gpu")
    kwargs = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
                  use_textline_orientation=False, return_word_box=True)
    if device != "cpu":
        kwargs["device"] = device
    return PaddleOCR(**kwargs)


_ocr_inst = None
_ocr_lock = threading.Lock()       # get_ocr 单例构造
_ocr_run_lock = threading.RLock()  # paddle 推理器非线程安全，predict 必须全程串行


def get_ocr():
    """进程级共享 OCR 实例：模型加载耗时长且占内存，全进程只加载一次、处处复用
    （此前每张未缓存图都会重新加载一次模型）。并发首调在锁内构造，仍只加载一次"""
    global _ocr_inst
    if _ocr_inst is None:
        with _ocr_lock:
            if _ocr_inst is None:
                _ocr_inst = ocr_factory()
    return _ocr_inst


def ocr_predict(img):
    """共享实例的串行 predict 入口：后台预热与草稿/导出线程并发调用同一推理器，
    会得到损坏的输入张量（Conv 收到空 tensor），所有 predict 都须经此锁"""
    with _ocr_run_lock:
        return get_ocr().predict(img)


def ocr_image_cached(ws, stem, img):
    cache = os.path.join(ws.cache_dir, f"{stem}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    with _ocr_run_lock:
        # 等锁期间同图可能已被其他线程算完落盘，命中即免二次 OCR
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        lines = ocr_image(img)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False)
    return lines


def ocr_image(img):
    """OCR 单图，返回行列表 [{box, text, score, words}]，大图切块后合并去重。
    内部经 ocr_predict 串行使用共享实例，不得在锁外直接调用 get_ocr().predict"""
    h, w = img.shape[:2]
    blocks = []
    if max(h, w) > 2400:
        step, overlap, axis = 2000, 240, (1 if h >= w else 0)
        total = h if axis == 0 else w
        pos = 0
        while pos < total:
            end = min(pos + step, total)
            if axis == 0:
                blocks.append((0, pos, w, end))
            else:
                blocks.append((pos, 0, end, h))
            if end >= total:
                break
            pos = end - overlap
    else:
        blocks.append((0, 0, w, h))

    lines = []
    for (x0, y0, x1, y1) in blocks:
        res = ocr_predict(img[y0:y1, x0:x1])
        d = res[0].json["res"]
        for i, t in enumerate(d["rec_texts"]):
            if not t.strip() or d["rec_scores"][i] < 0.5:
                continue
            b = np.array(d["rec_boxes"][i], dtype=np.int32)
            box = [int(b[0]) + x0, int(b[1]) + y0, int(b[2]) + x0, int(b[3]) + y0]
            words = []
            tw = d.get("text_word")
            tb = d.get("text_word_boxes")
            if tw and tb and i < len(tw) and i < len(tb):
                for wd, wb in zip(tw[i], tb[i]):
                    wb = np.array(wb, dtype=np.int32)
                    words.append({"w": wd, "box": [int(wb[0]) + x0, int(wb[1]) + y0,
                                                   int(wb[2]) + x0, int(wb[3]) + y0]})
            lines.append({"box": box, "text": t, "score": float(d["rec_scores"][i]), "words": words})
    return dedup_lines(lines)


def dedup_lines(lines):
    out = []
    for ln in sorted(lines, key=lambda x: -x["score"]):
        dup = False
        for kept in out:
            if kept["text"] == ln["text"] and iou(kept["box"], ln["box"]) > 0.55:
                dup = True
                break
        if not dup:
            out.append(ln)
    out.sort(key=lambda x: (x["box"][1], x["box"][0]))
    return out


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = min(ax2, bx2) - max(ax1, bx1)
    ih = min(ay2, by2) - max(ay1, by1)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / (area_a + area_b - inter)


# ---------------- 头像吸附（点击 → 圆） ----------------

def snap_avatar(img, x, y):
    """在点击点附近吸附头像，返回 (cx, cy, r) 或 None。
    先做局部连通域（贴合任意形状头像的外接圆），失败再试霍夫圆（头像与背景粘连时）。"""
    h, w = img.shape[:2]
    half = 240
    x0, y0 = max(0, x - half), max(0, y - half)
    x1, y1 = min(w, x + half), min(h, y + half)
    roi = img[y0:y1, x0:x1]
    if roi.size == 0:
        return None

    # 局部背景：ROI 外圈 8px 环带中值
    ring = np.concatenate([roi[:8].reshape(-1, 3), roi[-8:].reshape(-1, 3),
                           roi[:, :8].reshape(-1, 3), roi[:, -8:].reshape(-1, 3)])
    bg = np.median(ring, axis=0).astype(np.int16)
    diff = np.max(np.abs(roi.astype(np.int16) - bg), axis=2)
    mask = (diff > 22).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    px, py = x - x0, y - y0
    best, best_d = None, 1e9
    for i in range(1, n):
        bx, by, bw, bh, area = stats[i]
        if area < 100 or bw < 12 or bh < 12:
            continue
        if not (0.45 <= bw / bh <= 2.4) or (bw + bh) / 2 > 220:
            continue
        inside = bx <= px <= bx + bw and by <= py <= by + bh
        # 点击点落在斑内优先；否则允许离斑边框很近（< 半径级别距离）的命中
        d = 0.0 if inside else np.hypot(max(bx - px, px - bx - bw, 0), max(by - py, py - by - bh, 0))
        if d > 40:
            continue
        ys, xs = np.where(labels[max(0, by - 2):by + bh + 2, max(0, bx - 2):bx + bw + 2] == i)
        if len(xs) < 30:
            continue
        xs = xs + max(0, bx - 2)
        ys = ys + max(0, by - 2)
        (cx, cy), r = cv2.minEnclosingCircle(np.stack([xs, ys], axis=1).astype(np.float32))
        if d < best_d:
            best_d, best = d, (cx + x0, cy + y0, r)
    if best:
        return best

    # 霍夫圆兜底
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1, minDist=120,
                               param1=120, param2=30, minRadius=9, maxRadius=180)
    if circles is None:
        return None
    best, best_d = None, 1e9
    for cx, cy, r in circles[0]:
        d = np.hypot(cx - px, cy - py)
        if d < max(r, 40) and d < best_d:
            best_d, best = d, (cx + x0, cy + y0, r)
    return best


def avatar_mean_color(img, avatar):
    cx, cy, r = avatar
    h, w = img.shape[:2]
    x1, y1 = int(max(0, cx - r)), int(max(0, cy - r))
    x2, y2 = int(min(w, cx + r)), int(min(h, cy + r))
    roi = img[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    return roi.reshape(-1, 3).mean(axis=0)


# ---------------- 全局映射 ----------------

def fnv1a(name):
    """FNV-1a 32 位(UTF-8 字节):与浏览器脚本 pinyinInitial 的哈希一致,
    保证两端对同一非中英名字映射出同一字母"""
    h = 2166136261
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def pick_letter(name):
    ch = name[0]
    if re.match(r"[\u4e00-\u9fff]", ch):
        py = lazy_pinyin(ch)
        if py and py[0] and py[0][0].isascii():
            return py[0][0].upper()
        return chr(ord("A") + fnv1a(name) % 26)
    if ch.isascii() and ch.isalpha():
        return ch.upper()
    return chr(ord("A") + fnv1a(name) % 26)


def exclude_list(ws):
    """排除名单（不打码/不进映射的用户名）：mapping.json 的 exclude 字段，缺省 MAA-Official"""
    exc = ws.mapping.get("exclude")
    return set(exc) if exc is not None else {OFFICIAL}


def hue_dist(a, b):
    return min(abs(a - b), 360 - abs(a - b))


def rgb_of_hue(hue_deg):
    rr, gg, bb = colorsys.hsv_to_rgb(hue_deg / 360, 0.62, 0.55)
    return [int(rr * 255), int(gg * 255), int(bb * 255)]


def pick_hue(used_all, used_same, avatar_hue=None):
    """选色相：头像均值色不与已分配冲突（全局 ≥36°、同字母 ≥90°）时优先采用，
    否则黄金角序列取首个满足者；冲突无解时（同字母太多挤在一起）取 d_all≥30 中
    同字母距离最大的退化解。used_all/used_same 为已占用色相列表"""
    if avatar_hue is not None and all(hue_dist(avatar_hue, u0) >= 36 for u0 in used_all) \
            and all(hue_dist(avatar_hue, u0) >= 90 for u0 in used_same):
        return avatar_hue
    chosen = None
    ok_any = None
    ok_same_d = -1.0
    best_hue, best_score = 0.0, -1.0
    for i in range(600):
        cand = (i * 137.508) % 360
        d_all = min((hue_dist(cand, u0) for u0 in used_all), default=999.0)
        d_same = min((hue_dist(cand, u0) for u0 in used_same), default=999.0)
        if d_all >= 36 and d_same >= 90:
            chosen = cand
            break
        if d_all >= 30 and d_same > ok_same_d:
            ok_any = cand
            ok_same_d = d_same
        score = min(d_all, 36) / 36 + min(d_same, 90) / 90
        if score > best_score:
            best_hue, best_score = cand, score
    return chosen if chosen is not None else (ok_any if ok_any is not None else best_hue)


def assign(ws, name, avatar_mean_bgr=None):
    mapping = ws.mapping
    if name in mapping["users"]:
        info = mapping["users"][name]
        info.setdefault("letter", pick_letter(name))  # 旧映射缺 letter 时补
        if info.get("hue") is None or "rgb" not in info:
            # renumber 对尚未导出过的名字只补 uid/letter，旧版映射也可能缺色：现场补齐
            used_all = [u.get("hue") for u in mapping["users"].values() if u.get("hue") is not None]
            used_same = [u.get("hue") for u in mapping["users"].values()
                         if u.get("hue") is not None and u.get("letter") == info["letter"]]
            hue = info.get("hue")
            if hue is None:
                hue = pick_hue(used_all, used_same)
                info["hue"] = round(hue, 1)
            info["rgb"] = rgb_of_hue(hue)
        return info
    uid = len(mapping["users"]) + 1
    letter = pick_letter(name)

    avatar_hue = None
    if avatar_mean_bgr is not None:
        b, g, r = [c / 255 for c in avatar_mean_bgr]
        hh, ss, vv = colorsys.rgb_to_hsv(r, g, b)
        if ss > 0.15:
            avatar_hue = hh * 360
    used_all = [u.get("hue") for u in mapping["users"].values() if u.get("hue") is not None]
    # 同字母用户额外拉开（≥90°）：同缩写必须一眼可辨
    used_same = [u.get("hue") for u in mapping["users"].values()
                 if u.get("hue") is not None and u.get("letter") == letter]
    hue_deg = pick_hue(used_all, used_same, avatar_hue)
    info = {"uid": uid, "letter": letter, "hue": round(hue_deg, 1),
            "rgb": rgb_of_hue(hue_deg)}
    mapping["users"][name] = info
    return info


def mapping_remove(ws, name):
    return ws.mapping["users"].pop(name, None) is not None


def renumber_by_position(ws):
    """按 图片序号 → 图内从上到下 重排用户编号（导出前调用）。
    排序键 = 用户最早出现的标注位置（draft 不算）；颜色/字母保持不变，只改 uid；
    未在任何图标注过的映射残留用户排在最后。返回重排的用户数。"""
    first = {}
    for fi, f in enumerate(ws.list_images()):
        stem = os.path.splitext(f)[0]
        for m in ws.load_marks(stem):
            if m.get("draft"):
                continue
            name = m.get("name")
            if not name or name in exclude_list(ws):
                continue
            pos = m.get("avatar") or m.get("name_box")
            y = pos[1] if pos else 0
            key = (fi, y)
            if name not in first or key < first[name]:
                first[name] = key
    ordered = sorted(first.items(), key=lambda kv: kv[1])
    new_users = {}
    for name, _ in ordered:
        info = dict(ws.mapping["users"].get(name) or {})
        info["uid"] = len(new_users) + 1
        info.setdefault("letter", pick_letter(name))
        new_users[name] = info
    for name, info in ws.mapping["users"].items():
        if name not in new_users:
            info = dict(info)
            info["uid"] = len(new_users) + 1
            new_users[name] = info
    ws.mapping["users"] = new_users
    ws.save_mapping()
    return len(ordered)


# ---------------- 绘制 ----------------

def draw_letter_circle(draw, cx, cy, r, rgb, letter):
    rr = max(6, r)
    draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=tuple(rgb))
    fs = rr * 1.15
    font = get_font(fs, bold=True)
    bb = draw.textbbox((0, 0), letter, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    if tw > rr * 1.5:
        font = get_font(fs * rr * 1.5 / tw, bold=True)
        bb = draw.textbbox((0, 0), letter, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
    draw.text((cx - tw / 2 - bb[0], cy - th / 2 - bb[1]), letter, font=font, fill=(255, 255, 255))


def line_bg_color(img, box):
    """框外一圈背景色（上下左右采样中值）"""
    x1, y1, x2, y2 = box
    h, w = img.shape[:2]
    samples = []
    for yy in (max(0, y1 - 4), min(h - 1, y2 + 3)):
        yy = int(np.clip(yy, 0, h - 1))
        xa, xb = int(np.clip(x1, 0, w - 1)), int(np.clip(x2, 0, w - 1))
        if xb > xa:
            samples.append(img[yy, xa:xb])
    for xx in (max(0, x1 - 5), min(w - 1, x2 + 4)):
        xx = int(np.clip(xx, 0, w - 1))
        ya, yb = int(np.clip(y1, 0, h - 1)), int(np.clip(y2, 0, h - 1))
        if yb > ya:
            samples.append(img[ya:yb, xx])
    if not samples:
        return np.array([255, 255, 255], np.uint8)
    allpx = np.concatenate(samples, axis=0)
    return np.median(allpx, axis=0).astype(np.uint8)


def text_pixel_color(img, box, bg):
    x1, y1, x2, y2 = box
    h, w = img.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w - 1, x2), min(h - 1, y2)
    if x2 <= x1 or y2 <= y1:
        return None, 0
    roi = img[y1:y2 + 1, x1:x2 + 1].astype(np.int16)
    diff = np.max(np.abs(roi - np.array(bg, dtype=np.int16)), axis=2)
    m = diff > 40
    if m.sum() < 8:
        return None, 0
    mean = roi[m].mean(axis=0)
    return mean, int(m.sum())


def is_gray_text(color):
    b, g, r = color
    mx, mn = max(b, g, r), min(b, g, r)
    v = (b + g + r) / 3
    return (mx - mn) < 48 and 95 < v < 205


def label_expand_ok(lines, box):
    """覆盖框右侧紧邻是否无其他文本：有（句中 @提及后的正文、同行的徽章等）则标签须
    缩字号适配原宽，不得右扩盖掉后文；右侧空白（如行首独立用户名）才允许右扩。
    逐 word 判定——句中 @提及的后文与框同属一个 OCR 行，仅看其他行会漏判"""
    if not lines:
        return True
    h = max(1, box[3] - box[1])

    def near_right(b):
        if b[0] >= box[0] - 2 and b[2] <= box[2] + 2:
            return False  # 组成本框自身的 word
        ov = min(box[3], b[3]) - max(box[1], b[1])
        if ov <= 0.5 * max(1, min(box[3] - box[1], b[3] - b[1])):
            return False
        return -4 <= b[0] - box[2] < h * 3

    for ln in lines:
        words = ln.get("words")
        if words:
            if any(near_right(w["box"]) for w in words):
                return False
        elif near_right(ln["box"]):
            return False
    return True


def cover_text(draw, box, pad, bg, label, color, fs_scale=0.78, expand=True):
    x1, y1, x2, y2 = box
    if not label:
        draw.rectangle([x1 - pad, y1 - pad, x2 + pad, y2 + pad], fill=tuple(int(c) for c in bg))
        return
    h0 = (y2 - y1) + 2 * pad
    size = min(max(9, h0 * fs_scale), 34)
    lb = draw.textbbox((0, 0), label, font=get_font(size))
    need = (lb[2] - lb[0]) + 2
    if need > x2 - x1:
        if expand:
            x2 = x1 + need  # 标签比原区域宽时覆盖区右扩，避免与后文重叠
        else:
            # 不允许右扩：缩字号至标签放得下（下限 6px）
            while size > 6:
                size -= 1
                lb = draw.textbbox((0, 0), label, font=get_font(size))
                need = (lb[2] - lb[0]) + 2
                if need <= x2 - x1:
                    break
    draw.rectangle([x1 - pad, y1 - pad, x2 + pad, y2 + pad], fill=tuple(int(c) for c in bg))
    font = get_font(size)
    bb = draw.textbbox((0, 0), label, font=font)
    th = bb[3] - bb[1]
    draw.text((x1 - pad + 1, (y1 + y2) / 2 - th / 2 - bb[1]), label,
              font=font, fill=tuple(int(c) for c in color))


# ---------------- 文本级自动打码（@提及 / 引用标题） ----------------

def words_join(words):
    s, spans = "", []
    for wd in words:
        spans.append((len(s), len(s) + len(wd["w"])))
        s += wd["w"]
    return s, spans


def span_box(words, start_ch, end_ch):
    s, spans = words_join(words)
    idxs = [i for i, (a, b) in enumerate(spans) if a < end_ch and b > start_ch]
    if not idxs:
        return None
    boxes = [words[i]["box"] for i in idxs]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def find_mentions(ln, known_names):
    """@ 后已知用户名最长前缀匹配，返回 [(name, box)]"""
    out = []
    words = ln["words"]
    if not words:
        return out
    s, _ = words_join(words)
    names = sorted(known_names, key=len, reverse=True)
    for m in re.finditer(r"@", s):
        rest = s[m.end():]
        for name in names:
            if not name:
                continue
            if rest.startswith(name):
                box = span_box(words, m.end(), m.end() + len(name))
                if box:
                    out.append((name, box))
                break
    return out


# ---------------- 导出 ----------------

def export_image(ws, stem, marks, img=None, auto_scan=True, make_compare=True):
    """按标注导出匿名图到 output/<stem>.png；返回 (out_img, stats)。
    draft=true 的标注跳过；name 在映射缺失时按头像均值色现场分配。"""
    if img is None:
        img = imread_u(ws.image_path(stem))
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    stats = {"circles": 0, "covers": 0, "mentions": 0, "refs": 0}

    # OCR 行提前加载：既用于自动扫描，也用于判断覆盖框右侧是否紧邻文本（决定缩字号还是右扩）
    lines = ocr_image_cached(ws, stem, img) if auto_scan else None

    def info_of(m):
        if not m.get("name"):
            return None
        av_mean = avatar_mean_color(img, m["avatar"]) if m.get("avatar") else None
        return assign(ws, m["name"], av_mean)

    real = [m for m in marks if not m.get("draft")]
    # 同一标注可同时含 avatar 与 name_box：assign/头像取色只算一次
    info_by = {id(m): info_of(m) for m in real}
    for m in real:
        if m.get("avatar"):
            cx, cy, r = m["avatar"]
            info = info_by[id(m)]
            rgb = info["rgb"] if info else (128, 128, 128)
            draw_letter_circle(draw, cx, cy, r * 1.03, rgb, info["letter"] if info else "?")
            stats["circles"] += 1
    for m in real:
        nb = m.get("name_box")
        if not nb:
            continue
        bg = line_bg_color(img, nb)
        info = info_by[id(m)]
        # 标签用该用户专属颜色（与头像圆一致，同网页版），不再沿用原文字颜色
        cover_text(draw, nb, 2, bg, f"用户{info['letter']}" if info else None,
                   info["rgb"] if info else (128, 128, 128),
                   expand=label_expand_ok(lines, nb) if lines else True)
        stats["covers"] += 1

    if auto_scan:
        excluded = exclude_list(ws)
        known = set(ws.mapping["users"].keys()) | excluded
        for ln in lines:
            for name, box in find_mentions(ln, known):
                if name in excluded:
                    continue
                # assign 兼带缺色补齐：仅作 @提及 的名字可能从未经过标注路径
                info = assign(ws, name)
                bg = line_bg_color(img, box)
                cover_text(draw, box, 2, bg, f"用户{info['letter']}", info["rgb"],
                           expand=label_expand_ok(lines, box))
                stats["mentions"] += 1
        for ln in lines:
            t = ln["text"].strip()
            if t not in known or t in excluded or RE_TIME.search(t) or is_blacklisted(t):
                continue
            if any(iou(ln["box"], m["name_box"]) > 0.5 for m in real if m.get("name_box")):
                continue
            bg = line_bg_color(img, ln["box"])
            color, _ = text_pixel_color(img, ln["box"], bg)  # 仅作灰字过滤，不再用于上色
            if color is None or not is_gray_text(color):
                continue
            info = assign(ws, t)
            cover_text(draw, ln["box"], 2, bg, f"用户{info['letter']}", info["rgb"],
                       expand=label_expand_ok(lines, ln["box"]))
            stats["refs"] += 1

    out_img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    imwrite_u(os.path.join(ws.out_dir, stem + ".png"), out_img)
    if make_compare:
        a, b = img, out_img
        sep = np.full((a.shape[0], 6, 3), 0, np.uint8) if a.shape[0] > 1200 else \
              np.full((6, a.shape[1], 3), 0, np.uint8)
        cmp_img = np.hstack([a, sep, b]) if a.shape[0] > 1200 else np.vstack([a, sep, b])
        imwrite_u(os.path.join(ws.cmp_dir, stem + ".png"), cmp_img)
    return out_img, stats


# ---------------- 自动预标注草稿（沿用全自动检测，仅供人工/AI 复核修改） ----------------

def detect_blobs(img, bg_bgr):
    diff = np.max(np.abs(img.astype(np.int16) - np.array(bg_bgr, dtype=np.int16)), axis=2)
    mask = (diff > 28).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    blobs, zones = [], []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw > 220 or bh > 220:
            zones.append([x, y, x + bw, y + bh])
            continue
        if area < 120 or bw < 12 or bh < 12:
            continue
        ar = bw / bh
        if not (0.55 <= ar <= 2.2):
            continue
        if not (22 <= (bw + bh) / 2 <= 170):
            continue
        ys, xs = np.where(labels[max(0, y - 2):y + bh + 2, max(0, x - 2):x + bw + 2] == i)
        if len(xs) < 30:
            continue
        xs = xs + max(0, x - 2)
        ys = ys + max(0, y - 2)
        (cx, cy), r = cv2.minEnclosingCircle(np.stack([xs, ys], axis=1).astype(np.float32))
        blobs.append({"cx": cx, "cy": cy, "r": r, "bbox": [x, y, x + bw, y + bh]})
    return blobs, zones


def in_zones(bbox, zones, margin=4):
    x1, y1, x2, y2 = bbox
    for zx1, zy1, zx2, zy2 in zones:
        if x1 >= zx1 - margin and y1 >= zy1 - margin and x2 <= zx2 + margin and y2 <= zy2 + margin:
            return True
    return False


def pair_avatar(blobs, line_box, line_h):
    x1, yy1, x2, yy2 = line_box
    best = None
    for bl in blobs:
        bx1, by1, bx2, by2 = bl["bbox"]
        ov_top = max(yy1, by1)
        ov_bot = min(yy2, by2)
        ov = max(0, ov_bot - ov_top) / max(1, yy2 - yy1)
        if ov < 0.35:
            continue
        if abs(yy1 - by1) > 0.9 * max(14, line_h):
            continue
        gap = x1 - bx2
        if not (-10 <= gap <= line_h * 1.4 + 16):
            continue
        if bx2 > x1 + 8:
            continue
        if not (14 <= bl["r"] * 2 <= 150):
            continue
        if best is None or gap < best[0]:
            best = (gap, bl)
    return best[1] if best else None


def extend_over_badge_words(ln, name_box, name_len_chars):
    words = ln["words"]
    if not words or name_box is None:
        return name_box
    h = max(12, name_box[3] - name_box[1])
    s, spans = words_join(words)
    idxs = [i for i, (a, b) in enumerate(spans) if a < name_len_chars and b > name_len_chars - 0]
    end_wi = max(idxs) if idxs else -1
    if end_wi < 0:
        return name_box
    box = list(name_box)
    cur = end_wi
    for _ in range(2):
        nxt = cur + 1
        while nxt < len(words) and words[nxt]["w"].strip() == "":
            nxt += 1
        if nxt >= len(words):
            break
        w = words[nxt]["w"]
        wb = words[nxt]["box"]
        gap = wb[0] - box[2]
        if gap > h * 1.5:
            break
        if not (RE_BADGE.match(w) or re.fullmatch(r"\d{1,2}", w) or w in ("UP", "LIVE")):
            break
        box[2] = max(box[2], wb[2])
        cur = nxt
    return box


def extract_username(ln, known_names):
    text = ln["text"]
    s, _ = words_join(ln["words"])
    seq = s if s else text
    words = ln["words"] if ln["words"] else [{"w": text, "box": ln["box"]}]
    if OFFICIAL_PREFIX.match(seq):
        return None, None, True
    for name in sorted(known_names, key=len, reverse=True):
        if len(name) >= 2 and seq.startswith(name):
            return name, extend_over_badge_words(ln, span_box(words, 0, len(name)), len(name)), False
    if words:
        w0 = words[0]["w"]
        m0 = re.search(r"(?i)lu(?:[0-9]{1,2}|[geh])$", w0)
        if m0 and len(w0) > 4 and not OFFICIAL_PREFIX.match(w0):
            name = seq[:m0.start()].strip()
            if 1 <= len(name) <= 30 and not name.startswith("@"):
                return name, extend_over_badge_words(ln, span_box(words, 0, m0.start()), m0.start()), False
    acc, end_ch = "", 0
    prev_blank = True
    for wd in words:
        w = wd["w"]
        if w.strip() == "":
            acc += w
            prev_blank = True
            continue
        if w != acc.strip() and (RE_BADGE.match(w) or (w in ("u", "U") and prev_blank)):
            break
        acc += w
        end_ch += len(w)
        prev_blank = False
    name = acc.strip()
    if name and 1 <= len(name) <= 30 and not OFFICIAL_PREFIX.match(name) and not name.startswith("@"):
        return name, extend_over_badge_words(ln, span_box(words, 0, end_ch), end_ch), False
    box_w = ln["box"][2] - ln["box"][0]
    line_h = max(1, ln["box"][3] - ln["box"][1])
    if 1 <= len(seq) <= 30 and not RE_BADGE.match(seq) and not seq.startswith("@") and box_w <= 8 * line_h:
        return seq.strip(), span_box(words, 0, len(seq)), False
    return None, None, False


def absorb_badge_lines(lines, name_box):
    if name_box is None:
        return name_box
    h = max(12, name_box[3] - name_box[1])
    box = list(name_box)
    for ln in lines:
        t = ln["text"].strip()
        if not (RE_BADGE.match(t) or t in ("UP", "LIVE") or re.fullmatch(r"\d{1,2}", t)):
            continue
        b = ln["box"]
        ov = min(box[3], b[3]) - max(box[1], b[1])
        if ov <= 0.5 * max(1, min(box[3] - box[1], b[3] - b[1])):
            continue
        if not (box[2] - 8 <= b[0] <= box[2] + h * 1.5):
            continue
        box[2] = max(box[2], b[2])
    return box


def auto_draft(ws, stem, img=None):
    """全自动检测生成草稿标注（draft=true，导出前需确认）。已知问题：嵌入 UI 截图内的
    按钮图标可能被误判为头像、浅色头像可能漏检——草稿只当底稿，逐条确认或删除。"""
    if img is None:
        img = imread_u(ws.image_path(stem))
    diff = img[::4, ::4].reshape(-1, 3) // 16
    quant, counts = np.unique(diff, axis=0, return_counts=True)
    bg_bgr = (quant[counts.argmax()] * 16 + 8).astype(np.uint8)
    lines = ocr_image_cached(ws, stem, img)
    blobs, zones = detect_blobs(img, bg_bgr)
    known = set(ws.mapping["users"].keys()) | exclude_list(ws)

    cands = {}
    used_block_lines = set()
    time_idx = [i for i, ln in enumerate(lines) if RE_TIME.search(ln["text"])]
    for ti, tidx in enumerate(time_idx):
        tline = lines[tidx]
        prev_bottom = lines[time_idx[ti - 1]]["box"][3] if ti > 0 else -1
        block = [ln for ln in lines
                 if prev_bottom < ln["box"][1] <= tline["box"][3] + 2
                 and ln["box"][3] <= tline["box"][3] + 6
                 and ln is not tline]
        if not block:
            continue
        block.sort(key=lambda x: x["box"][1])
        clusters = []
        for ln in block:
            h_ref = max(14, ln["box"][3] - ln["box"][1])
            if clusters and ln["box"][1] - clusters[-1][0]["box"][1] < 0.45 * h_ref:
                clusters[-1].append(ln)
            else:
                clusters.append([ln])
        for cluster in clusters:
            top = min(cluster, key=lambda x: x["box"][0])
            if is_blacklisted(top["text"]):
                continue
            h = top["box"][3] - top["box"][1]
            av = pair_avatar(blobs, top["box"], max(h, 14))
            if av:
                cands[id(top)] = (top, av)
                used_block_lines.update(id(ln) for ln in block)
                break
    for ln in lines:
        if id(ln) in cands or id(ln) in used_block_lines or RE_TIME.search(ln["text"]) or is_blacklisted(ln["text"]):
            continue
        h = ln["box"][3] - ln["box"][1]
        if h < 8 or h > 60:
            continue
        if ln["box"][2] - ln["box"][0] > 260:
            continue
        bg = line_bg_color(img, ln["box"])
        if np.max(np.abs(bg.astype(np.int16) - np.array(bg_bgr, dtype=np.int16))) > 26:
            continue
        color, npx = text_pixel_color(img, ln["box"], bg)
        if color is None or not is_gray_text(color):
            continue
        av = pair_avatar(blobs, ln["box"], h)
        if av and not in_zones(av["bbox"], zones):
            cands[id(ln)] = (ln, av)

    marks = []
    for ln, av in cands.values():
        name, name_box, is_off = extract_username(ln, known)
        if is_off or not name or name_box is None:
            continue
        name_box = absorb_badge_lines(lines, name_box)
        marks.append({"avatar": [round(av["cx"], 1), round(av["cy"], 1), round(av["r"], 1)],
                      "name_box": [int(v) for v in name_box],
                      "name": name, "draft": True})
    return marks
