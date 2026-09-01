# -*- coding: utf-8 -*-
"""
截图匿名化半自动标注界面
用法: python ui.py [图片目录]
操作:
- 左键单击      吸附头像圆（局部连通域，失败退霍夫圆），OCR 预填名字供审核
- 左键拖拽      框选用户名/提及文本覆盖框，OCR 预填名字供审核
- 双击标注      修改该标注的名字
- 右键单击      拆分删除：点在文字框上只删框（保留头像圆），点在圆上只删圆
- 右键拖拽      框选头像圆（修正自动检测过小/偏移的圆；框内有旧圆则只替换圆）
- 拖动文字框    平移覆盖框微调位置
- Ctrl+Z        撤销最近一次添加/删除
- 滚轮 / Alt+滚轮  垂直滚动 / 缩放（0.1~3.0）
- Ctrl+S 保存标注   E 导出当前   A 全部导出   D 生成自动草稿   T 采纳全部草稿
名字输入留空直接确定 = 纯涂色覆盖（不进映射）；选 ｢MAA-Official｣ = 放弃本次标注。
同一名字跨图自动同色同编号（mapping.json 全局映射）；导出时自动扫描 @提及 与灰字引用标题。
"""
import os
import sys
import copy
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core


class AskNameDialog(tk.Toplevel):
    """名字输入：下拉选已知名 / 手输新名 / 留空=纯涂色 / 选 MAA-Official=放弃"""

    def __init__(self, master, known_names, prefill=""):
        super().__init__(master)
        self.title("标注用户名")
        self.result = None
        self.grab_set()
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="用户名（留空=纯涂色不进映射；选 MAA-Official=放弃标注）").pack(anchor="w")
        self.var = tk.StringVar(value=prefill)
        combo = ttk.Combobox(frm, textvariable=self.var, values=list(known_names), width=40)
        combo.pack(fill="x", pady=6)
        combo.select_range(0, "end")
        combo.focus_set()
        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        ttk.Button(btns, text="确定", command=self._ok).pack(side="left", padx=2)
        ttk.Button(btns, text="放弃标注", command=self._cancel).pack(side="left", padx=2)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.wait_window()

    def _ok(self):
        self.result = self.var.get().strip()
        self.destroy()

    def _cancel(self):
        self.result = core.OFFICIAL
        self.destroy()


class AnnotatorUI:
    ZOOM_MIN, ZOOM_MAX = 0.1, 3.0

    def __init__(self, root, src_dir=None):
        self.root = root
        root.title("截图匿名化标注")
        self.ws = None
        self.stems = []
        self.cur = None          # 当前 stem
        self.img = None          # BGR 原图
        self.marks = []
        self.scale = 0.5
        self.photo = None
        self.undo_stack = []
        self.drag_start = None
        self.sel_idx = None
        self._busy = False       # 后台任务（OCR/草稿/导出）运行中，忽略标注输入
        self._build()

    # ---------- UI 骨架 ----------
    def _build(self):
        top = ttk.Frame(self.root, padding=4)
        top.pack(fill="x")
        ttk.Button(top, text="打开目录", command=self.open_dir).pack(side="left")
        ttk.Button(top, text="◀ 上一张", command=lambda: self.step(-1)).pack(side="left", padx=2)
        ttk.Button(top, text="下一张 ▶", command=lambda: self.step(1)).pack(side="left", padx=2)
        ttk.Button(top, text="保存标注 (Ctrl+S)", command=self.save_marks).pack(side="left", padx=8)
        ttk.Button(top, text="导出当前 (E)", command=self.export_current).pack(side="left", padx=2)
        ttk.Button(top, text="全部导出 (A)", command=self.export_all).pack(side="left", padx=2)
        ttk.Button(top, text="自动草稿 (D)", command=self.run_draft).pack(side="left", padx=8)
        ttk.Button(top, text="采纳全部草稿 (T)", command=self.accept_drafts).pack(side="left", padx=2)
        ttk.Label(top, text="排除:").pack(side="left", padx=(8, 0))
        self.exclude_var = tk.StringVar()
        exc_entry = ttk.Entry(top, textvariable=self.exclude_var, width=26)
        exc_entry.pack(side="left", padx=2)
        exc_entry.bind("<FocusOut>", self.save_exclude)
        exc_entry.bind("<Return>", self.save_exclude)

        self.status = tk.StringVar(value="先打开图片目录")
        ttk.Label(self.root, textvariable=self.status, padding=(6, 0)).pack(fill="x")

        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(main, width=28, exportselection=False)
        self.listbox.pack(side="left", fill="y")
        self.listbox.bind("<<ListboxSelect>>", self.on_pick)
        self.canvas = tk.Canvas(main, bg="#404040")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-2>", self.on_press)
        self.canvas.bind("<B2-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-2>", self.on_release)
        self.canvas.bind("<ButtonPress-3>", self.on_right_press)
        self.canvas.bind("<B3-Motion>", self.on_right_motion)
        self.canvas.bind("<ButtonRelease-3>", self.on_right)
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Alt-MouseWheel>", self.on_wheel_zoom)
        for seq, fn in (("<Control-s>", self.save_marks), ("<Control-z>", self.undo),
                        ("<e>", self.export_current), ("<a>", self.export_all),
                        ("<d>", self.run_draft), ("<t>", self.accept_drafts)):
            self.root.bind(seq, lambda e, f=fn: None if self._busy else f())

    # ---------- 目录与图片 ----------
    def open_dir(self):
        d = filedialog.askdirectory(title="选择图片目录")
        if not d:
            return
        self.load_workspace(d)

    def load_workspace(self, d):
        self.ws = core.Workspace(d)
        # 后台预载 OCR 模型：首次加载可达几十秒，不能等首次点击时才在 UI 线程里发生
        threading.Thread(target=core.get_ocr, daemon=True).start()
        self.exclude_var.set(",".join(sorted(core.exclude_list(self.ws))))
        self.stems = [os.path.splitext(f)[0] for f in self.ws.list_images()]
        self.listbox.delete(0, "end")
        for s in self.stems:
            tag = " [已标注]" if os.path.exists(self.ws.marks_path(s)) else ""
            self.listbox.insert("end", s + tag)
        if self.stems:
            self.listbox.selection_set(0)
            self.on_pick()
        self.status.set(f"{d}  共 {len(self.stems)} 张, 映射 {len(self.ws.mapping['users'])} 用户")

    def save_exclude(self, _ev=None):
        """排除名单输入框（逗号分隔）→ mapping.json 的 exclude 字段，导出时生效"""
        if not self.ws:
            return
        names = [s.strip() for s in
                 self.exclude_var.get().replace("，", ",").replace("；", ",").replace(";", ",").split(",")
                 if s.strip()]
        self.ws.mapping["exclude"] = names
        self.ws.save_mapping()

    def on_pick(self, _=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.load_image(self.stems[sel[0]])

    def step(self, delta):
        if not self.stems:
            return
        i = self.stems.index(self.cur) if self.cur in self.stems else -1
        ni = max(0, min(len(self.stems) - 1, i + delta))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(ni)
        self.listbox.see(ni)
        self.load_image(self.stems[ni])

    def load_image(self, stem):
        self.save_marks(silent=True)
        self.cur = stem
        self.img = core.imread_u(self.ws.image_path(stem))
        self.marks = self.ws.load_marks(stem)
        self.undo_stack = []
        self.sel_idx = None
        self.fit_scale()
        self.render()
        self.warm_ocr(stem, self.img)

    def warm_ocr(self, stem, img):
        """后台预热整图 OCR（结果落盘缓存），点头像预填名字时即时可用"""
        if os.path.exists(os.path.join(self.ws.cache_dir, stem + ".json")):
            return
        t = getattr(self, "_ocr_thread", None)
        if t is not None and t.is_alive() and getattr(self, "_ocr_stem", None) == stem:
            return
        self._ocr_stem = stem
        self._ocr_thread = threading.Thread(
            target=lambda: core.ocr_image_cached(self.ws, stem, img), daemon=True)
        self._ocr_thread.start()

    def _ensure_ocr_lines(self):
        t = getattr(self, "_ocr_thread", None)
        if t is not None and t.is_alive():
            self.status.set("OCR 全图识别中（首次打开该图需等待，结果有缓存）...")
            while t.is_alive():
                t.join(0.1)
                self.root.update_idletasks()  # 等待中保持界面重绘，不整窗冻结
        return core.ocr_image_cached(self.ws, self.cur, self.img)

    def fit_scale(self):
        h, w = self.img.shape[:2]
        cw = max(self.canvas.winfo_width(), 800)
        ch = max(self.canvas.winfo_height(), 600)
        self.scale = max(self.ZOOM_MIN, min(1.5, min(cw / w, ch / h)))

    # ---------- 渲染 ----------
    def render(self, keep_view=False):
        if self.img is None:
            return
        if not keep_view:
            self.view_x = self.canvas.canvasx(0)
            self.view_y = self.canvas.canvasy(0)
        disp = cv2.resize(self.img, None, fx=self.scale, fy=self.scale,
                          interpolation=cv2.INTER_AREA if self.scale < 1 else cv2.INTER_NEAREST)
        self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        h, w = self.img.shape[:2]
        self.canvas.config(scrollregion=(0, 0, w * self.scale, h * self.scale))
        for i, m in enumerate(self.marks):
            draft = m.get("draft")
            color = "#909090" if draft else ("#00E5FF" if i == self.sel_idx else "#22DD44")
            width = 1 if draft else 2
            if m.get("avatar"):
                cx, cy, r = m["avatar"]
                self.canvas.create_oval((cx - r) * self.scale, (cy - r) * self.scale,
                                        (cx + r) * self.scale, (cy + r) * self.scale,
                                        outline=color, width=width, dash=(4, 3) if draft else None)
                if m.get("name"):
                    self.canvas.create_text(cx * self.scale, (cy - r) * self.scale - 8,
                                            text=m["name"][:16], fill=color,
                                            font=("Microsoft YaHei", 9))
            nb = m.get("name_box")
            if nb:
                self.canvas.create_rectangle(nb[0] * self.scale, nb[1] * self.scale,
                                             nb[2] * self.scale, nb[3] * self.scale,
                                             outline=color, width=width, dash=(4, 3) if draft else None)
        n_real = sum(1 for m in self.marks if not m.get("draft"))
        n_draft = len(self.marks) - n_real
        self.status.set(f"{self.cur}  缩放 {self.scale:.2f}  标注 {n_real}  草稿 {n_draft}"
                        f"  映射 {len(self.ws.mapping['users'])} 用户  "
                        f"左键点=头像 拖=框名 右键=删 双击=改名")

    # ---------- 坐标换算 ----------
    def to_img(self, ex, ey):
        return (self.canvas.canvasx(ex) / self.scale, self.canvas.canvasy(ey) / self.scale)

    def hit_mark(self, x, y):
        """返回 (index, part)，part 为 'name_box' 或 'avatar'；未命中返回 None。
        点落在文字框内部时优先命中框（框目标小，避免被相邻头像圆抢占）"""
        for i, m in enumerate(self.marks):
            nb = m.get("name_box")
            if nb and nb[0] - 4 <= x <= nb[2] + 4 and nb[1] - 4 <= y <= nb[3] + 4:
                return i, "name_box"
        best, best_part, best_d = None, None, 1e9
        for i, m in enumerate(self.marks):
            if m.get("avatar"):
                cx, cy, r = m["avatar"]
                dist = np.hypot(x - cx, y - cy)
                if dist <= r * 1.15 and abs(dist - r) < best_d:
                    best, best_part, best_d = i, "avatar", abs(dist - r)
        if best is None:
            return None
        return best, best_part

    # ---------- 交互 ----------
    def on_press(self, e):
        if self._busy:
            return
        self.drag_start = (e.x, e.y)
        self._move_state = None
        if e.num == 2:  # 中键拖 = 平移视图
            self.canvas.scan_mark(e.x, e.y)
            return
        # 左键按下落在文字框内 → 准备拖动平移该框（无位移则仍是普通点击）
        if self.img is not None:
            x, y = self.to_img(e.x, e.y)
            h = self.hit_mark(x, y)
            if h and h[1] == "name_box":
                nb = self.marks[h[0]].get("name_box")
                if nb:
                    self._move_state = {"i": h[0], "orig": list(nb), "start": (e.x, e.y),
                                        "undo": copy.deepcopy(self.marks), "moved": False}

    def on_motion(self, e):
        st = getattr(self, "_move_state", None)
        if st:
            if abs(e.x - st["start"][0]) + abs(e.y - st["start"][1]) > 5:
                dx = (e.x - st["start"][0]) / self.scale
                dy = (e.y - st["start"][1]) / self.scale
                o = st["orig"]
                st["moved"] = True
                self.marks[st["i"]]["name_box"] = [o[0] + dx, o[1] + dy, o[2] + dx, o[3] + dy]
                self.render()
            return
        if e.num == 2 or getattr(self, "_panning", False):
            self._panning = True
            self.canvas.scan_dragto(e.x, e.y, gain=1)
            return
        if self.drag_start and np.hypot(e.x - self.drag_start[0], e.y - self.drag_start[1]) > 6:
            # 左键拖 = 框选预览
            if hasattr(self, "_rubber"):
                self.canvas.delete(self._rubber)
            self._rubber = self.canvas.create_rectangle(
                self.drag_start[0], self.drag_start[1], e.x, e.y,
                outline="#FFAA00", width=2, dash=(5, 3))

    def on_release(self, e):
        if self._busy:
            return
        if e.num == 2:  # 中键松开：仅复位平移，不做标注
            self.drag_start = None
            self._panning = False
            return
        st = getattr(self, "_move_state", None)
        if st:
            self._move_state = None
            self.drag_start = None
            if st["moved"]:
                self.undo_stack.append(st["undo"])
                if len(self.undo_stack) > 100:
                    self.undo_stack.pop(0)
                self.status.set("已移动文字覆盖框，Ctrl+Z 可撤销")
                return
            # 无位移：落回普通点击流程（选中/草稿采纳）
        start, panning = self.drag_start, getattr(self, "_panning", False)
        self.drag_start = None
        self._panning = False
        if hasattr(self, "_rubber"):
            self.canvas.delete(self._rubber)
            del self._rubber
        if not start or self.img is None or panning:
            return
        # 框选意图按屏幕位移判定（与缩放无关），放大视图后手抖不吞掉单击
        if np.hypot(e.x - start[0], e.y - start[1]) > 10:
            x1, y1 = self.to_img(*start)
            x2, y2 = self.to_img(e.x, e.y)
            if abs(x2 - x1) < 8 or abs(y2 - y1) < 6:
                self.status.set("框选太小，请框住完整的用户名/提及文本")
                return
            nb = [int(min(x1, x2)), int(min(y1, y2)), int(max(x1, x2)), int(max(y1, y2))]
            self.add_mark({"avatar": None, "name_box": nb, "name": None})
            return
        x, y = self.to_img(e.x, e.y)
        h = self.hit_mark(x, y)
        if h is not None:
            i = h[0]
            m = self.marks[i]
            if m.get("draft"):
                # 单击草稿 = 采纳这一条：OCR 预填名弹窗确认后转正
                r = AskNameDialog(self.root, self.ws.mapping["users"].keys(), m.get("name") or "")
                if r.result == core.OFFICIAL:
                    return
                self._push_undo()
                m.pop("draft", None)
                m["name"] = r.result or None
            self.sel_idx = i
            self.render()
            return
        av = core.snap_avatar(self.img, int(x), int(y))
        if av:
            self.add_mark({"avatar": [round(av[0], 1), round(av[1], 1), round(av[2], 1)],
                           "name_box": None, "name": None})
        else:
            self.status.set("此处未吸附到头像（左键拖拽可框选纯文本覆盖）")

    def on_double(self, e):
        if self._busy:
            return
        x, y = self.to_img(e.x, e.y)
        h = self.hit_mark(x, y)
        if h is None:
            return
        m = self.marks[h[0]]
        r = AskNameDialog(self.root, self.ws.mapping["users"].keys(), m.get("name") or "")
        if r.result == core.OFFICIAL:
            return
        if m.get("name") == (r.result or None):
            return
        self._push_undo()
        m["name"] = r.result or None
        self.render()

    def on_right_press(self, e):
        self.rdrag_start = (e.x, e.y)

    def on_right_motion(self, e):
        if getattr(self, "rdrag_start", None) and \
                np.hypot(e.x - self.rdrag_start[0], e.y - self.rdrag_start[1]) > 10:
            if hasattr(self, "_rubber_r"):
                self.canvas.delete(self._rubber_r)
            self._rubber_r = self.canvas.create_rectangle(
                self.rdrag_start[0], self.rdrag_start[1], e.x, e.y,
                outline="#FF5555", width=2, dash=(5, 3))

    def on_right(self, e):
        """右键单击 = 删除最近标注；右键拖拽 = 框选头像圆（框中心为圆心、长边为直径，
        框内已有头像标注时只替换其圆，用于修正自动检测过小/偏移的圆）"""
        if self._busy:
            return
        if self.img is None:
            return
        start = getattr(self, "rdrag_start", None)
        self.rdrag_start = None
        if hasattr(self, "_rubber_r"):
            self.canvas.delete(self._rubber_r)
            del self._rubber_r
        if start is None:
            return
        if np.hypot(e.x - start[0], e.y - start[1]) > 10:
            x1, y1 = self.to_img(*start)
            x2, y2 = self.to_img(e.x, e.y)
            bx1, by1, bx2, by2 = int(min(x1, x2)), int(min(y1, y2)), int(max(x1, x2)), int(max(y1, y2))
            if bx2 - bx1 < 8 or by2 - by1 < 8:
                self.status.set("框选太小，请框住整个头像")
                return
            cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
            r = max(bx2 - bx1, by2 - by1) / 2
            target = next((i for i, m in enumerate(self.marks)
                           if m.get("avatar") and bx1 <= m["avatar"][0] <= bx2
                           and by1 <= m["avatar"][1] <= by2), None)
            if target is not None:
                self._push_undo()
                self.marks[target]["avatar"] = [round(cx, 1), round(cy, 1), round(r, 1)]
                self.sel_idx = target
                self.status.set(f"已替换头像圆 (r={r:.0f})，Ctrl+Z 可撤销")
                self.render()
            else:
                self.add_mark({"avatar": [round(cx, 1), round(cy, 1), round(r, 1)],
                               "name_box": None, "name": None})
            return
        # 右键单击：按点击位置拆分删除——点在文字框上只删框（保留头像圆），
        # 点在圆上只删圆（保留文字框）；都不精确命中时删除选中/最近的整条标注
        x, y = self.to_img(e.x, e.y)
        h = self.hit_mark(x, y)
        if h is None:
            i = self.sel_idx
            if i is None:
                return
            self._push_undo()
            del self.marks[i]
        else:
            i, part = h
            self._push_undo()
            m = self.marks[i]
            m[part if part == "avatar" else "name_box"] = None
            if not m.get("avatar") and not m.get("name_box"):
                del self.marks[i]
        self.sel_idx = None
        self.render()

    def _push_undo(self):
        """任何修改前存整份标注快照（深拷贝），Ctrl+Z 逐步回退"""
        self.undo_stack.append(copy.deepcopy(self.marks))
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)

    def undo(self, _=None):
        if not self.undo_stack:
            self.status.set("没有可撤销的操作（撤销只在本次会话内有效）")
            return
        self.marks = self.undo_stack.pop()
        self.sel_idx = None
        self.render()

    def add_mark(self, m):
        if m.get("name_box"):
            prefill = self.guess_name(m["name_box"])
        elif m.get("avatar"):
            prefill = self.guess_avatar_name(m["avatar"])
        else:
            prefill = ""
        r = AskNameDialog(self.root, self.ws.mapping["users"].keys(), prefill)
        if r.result == core.OFFICIAL:
            return
        m["name"] = r.result or None
        self._push_undo()
        self.marks.append(m)
        self.sel_idx = len(self.marks) - 1
        self.render()

    def guess_avatar_name(self, av):
        """OCR 缓存中找头像右侧、顶部对齐的用户名行，做徽章截断后作为预填名"""
        cx, cy, r = av
        top = cy - r
        best = None
        for ln in self._ensure_ocr_lines():
            b = ln["box"]
            h = b[3] - b[1]
            gap = b[0] - (cx + r)
            if not (-6 <= gap <= r * 2.5):
                continue
            # 用户名行与头像顶部对齐（同 pair_avatar 判据），过高的行是内容
            if abs(b[1] - top) > max(0.9 * max(14, h), r * 0.5) or h > r * 1.2:
                continue
            if core.is_blacklisted(ln["text"]) or core.RE_TIME.search(ln["text"]):
                continue
            if best is None or b[0] < best["box"][0]:
                best = ln
        if best is None:
            return ""
        name, _, is_off = core.extract_username(
            best, set(self.ws.mapping["users"].keys()) | {core.OFFICIAL})
        if is_off:
            return core.OFFICIAL  # 预填 MAA-Official，确认即放弃标注
        return name or ""

    def guess_name(self, box):
        """框选区域预填名：先查整图 OCR 缓存里与框重叠的行，未命中再对框跑一次小图 OCR"""
        best, best_iou = None, 0.5
        for ln in self._ensure_ocr_lines():
            v = core.iou(ln["box"], box)
            if v > best_iou:
                best, best_iou = ln, v
        if best is not None:
            name, _, is_off = core.extract_username(
                best, set(self.ws.mapping["users"].keys()) | {core.OFFICIAL})
            return name if name and not is_off else ""
        x1, y1, x2, y2 = [int(v) for v in box]
        pad = 6
        crop = self.img[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]
        if crop.size == 0 or crop.shape[0] < 6:
            return ""
        # 串行入口：模型在打开目录时已后台预载；极端情况下仍加载中则此处等它完成
        res = core.ocr_predict(crop)
        d = res[0].json["res"]
        if not d["rec_texts"]:
            return ""
        i = int(np.argmax(d["rec_scores"]))
        t = d["rec_texts"][i].strip()
        return t[:30] if 0 < len(t) <= 30 else ""

    # ---------- 滚动缩放 ----------
    def on_wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def on_wheel_zoom(self, e):
        f = 1.15 if e.delta > 0 else 1 / 1.15
        self.scale = max(self.ZOOM_MIN, min(self.ZOOM_MAX, self.scale * f))
        self.render(keep_view=True)

    # ---------- 草稿 ----------
    def run_draft(self, _=None):
        if not self.ws or self._busy:
            return
        self.save_marks(silent=True)
        self._busy = True
        stem, img = self.cur, self.img
        self.status.set("生成自动草稿中（OCR + 检测；模型加载只发生一次）...")
        self.root.update_idletasks()

        def work():
            marks = core.auto_draft(self.ws, stem, img)
            self.root.after(0, lambda: self._draft_done(stem, marks))

        threading.Thread(target=work, daemon=True).start()

    def _draft_done(self, stem, marks):
        """草稿结果回 UI 线程套用；等待期间切换过图片则丢弃"""
        self._busy = False
        if stem != self.cur:
            self.status.set("自动草稿完成，但期间已切换图片，结果丢弃")
            return
        have = {tuple(m["avatar"]) for m in self.marks if m.get("avatar")}
        added = [m for m in marks if tuple(m["avatar"]) not in have]
        if not added:
            self.status.set("没有新的自动草稿")
            return
        self._push_undo()
        self.marks.extend(added)
        self.status.set(f"自动草稿 +{len(added)}（灰色虚线，逐个确认：双击改名/右键删除，或 T 全部采纳）")
        self.render()

    def accept_drafts(self, _=None):
        if not any(m.get("draft") for m in self.marks):
            self.status.set("没有草稿可采纳")
            return
        self._push_undo()
        n = 0
        for m in self.marks:
            if m.pop("draft", None):
                n += 1
        self.status.set(f"已采纳 {n} 条草稿（Ctrl+Z 可整批撤销）")
        self.render()

    # ---------- 保存导出 ----------
    def save_marks(self, _=None, silent=False):
        if not self.ws or not self.cur:
            return
        self.ws.save_marks(self.cur, self.marks)
        self.ws.save_mapping()
        if not silent:
            self.status.set(f"已保存 {self.cur} 标注与映射")
        # 刷新列表里的已标注记号
        if self.cur in self.stems:
            i = self.stems.index(self.cur)
            if not self.listbox.get(i).endswith(" [已标注]"):
                self.listbox.delete(i)
                self.listbox.insert(i, self.cur + " [已标注]")

    def export_current(self, _=None):
        if not self.ws or self._busy:
            return
        self.save_marks(silent=True)
        self._busy = True
        stem, marks, img = self.cur, list(self.marks), self.img
        self.status.set("导出中（首次需加载 OCR 模型，请稍候）...")
        self.root.update_idletasks()

        def work():
            core.renumber_by_position(self.ws)
            _, st = core.export_image(self.ws, stem, marks, img=img)
            self.ws.save_mapping()
            msg = (f"已导出 {stem}: 圆{st['circles']} 覆盖{st['covers']} "
                   f"@{st['mentions']} 引用{st['refs']}（编号已按图序+位置重排）"
                   f" → output/{stem}.png")
            self.root.after(0, lambda: self._export_done(msg))

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, msg):
        self._busy = False
        self.status.set(msg)

    def export_all(self, _=None):
        if not self.ws or self._busy:
            return
        self.save_marks(silent=True)
        self._busy = True
        stems = list(self.stems)
        self.status.set("全部导出中...")
        self.root.update_idletasks()

        def work():
            core.renumber_by_position(self.ws)
            total = 0
            for stem in stems:
                marks = self.ws.load_marks(stem)
                if not marks:
                    continue
                core.export_image(self.ws, stem, marks)
                total += 1
                self.root.after(0, lambda n=total, s=stem: self.status.set(
                    f"全部导出中... 已完成 {n} 张（{s}）"))
            self.ws.save_mapping()
            self.root.after(0, lambda: self._export_done(
                f"全部导出完成: {total}/{len(self.stems)} 张（编号已按图序+位置重排） → output/"))
            self.root.after(0, lambda: messagebox.showinfo("完成", f"已导出 {total} 张到 output/"))

        threading.Thread(target=work, daemon=True).start()


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    root.geometry("1280x860")
    app = AnnotatorUI(root, src)
    if src:
        app.load_workspace(src)
    root.mainloop()


if __name__ == "__main__":
    main()
