# -*- coding: utf-8 -*-
"""
截图匿名化 MCP server（stdio），供 AI 编程调用同一套半自动流程
依赖: pip install mcp
任意 MCP 客户端的 mcpServers 配置（路径按实际存放位置填写）:
  "anonymizer": {"command": "python", "args": ["<anonymizer 目录>/mcp_server.py"]}
AI 建议工作流:
  1) list_images 看目录
  2) auto_draft 生成某图草稿（全自动检测，含已知误检风险）
  3) 读导出的 compare 图（若已 export）或原图，视觉核对草稿：删除误检（嵌入 UI 截图内的
     按钮/复选框圆）、补漏检（浅色头像/整楼漏盖）
  4) 需要新增头像时用 snap_avatar 验证点击吸附；set_marks 保存修正后的全部标注
  5) export 出图（自动扫描 @提及/引用标题），再读 compare 图验收
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("anonymizer")


def _ws(src_dir):
    return core.Workspace(src_dir)


@mcp.tool()
def list_images(src_dir: str) -> str:
    """列出目录下全部 png 及其标注/导出状态与映射用户数"""
    ws = _ws(src_dir)
    items = []
    for f in ws.list_images():
        stem = os.path.splitext(f)[0]
        n = len(ws.load_marks(stem))
        done = os.path.exists(os.path.join(ws.out_dir, stem + ".png"))
        items.append({"stem": stem, "marks": n, "exported": done})
    return json.dumps({"images": items, "users": len(ws.mapping["users"])},
                      ensure_ascii=False)


@mcp.tool()
def get_marks(src_dir: str, stem: str) -> str:
    """读取某图的标注列表"""
    return json.dumps({"marks": _ws(src_dir).load_marks(stem)}, ensure_ascii=False)


@mcp.tool()
def set_marks(src_dir: str, stem: str, marks: list) -> str:
    """覆盖保存某图全部标注。元素格式:
    {"avatar": [cx,cy,r]|null, "name_box": [x1,y1,x2,y2]|null, "name": "用户名"|null}
    name 留 null = 纯涂色不进映射；草稿需去掉 draft 字段或直接不写"""
    ws = _ws(src_dir)
    ws.save_marks(stem, marks)
    ws.save_mapping()
    return f"saved {len(marks)} marks for {stem}"


@mcp.tool()
def snap_avatar(src_dir: str, stem: str, x: int, y: int) -> str:
    """在点击点附近吸附头像圆（局部连通域，失败退霍夫圆），返回 [cx,cy,r] 或 null"""
    img = core.imread_u(_ws(src_dir).image_path(stem))
    av = core.snap_avatar(img, int(x), int(y))
    return json.dumps({"avatar": list(av) if av else None})


@mcp.tool()
def auto_draft(src_dir: str, stem: str) -> str:
    """全自动检测生成草稿标注（draft=true）。误检风险：嵌入 UI 截图内按钮被当头像；
    漏检风险：浅色头像。仅作底稿，须逐条视觉核对后再去 draft 采纳"""
    ws = _ws(src_dir)
    marks = core.auto_draft(ws, stem)
    old = ws.load_marks(stem)
    have = {tuple(m.get("avatar") or ()) for m in old}
    merged = old + [m for m in marks if tuple(m.get("avatar") or ()) not in have]
    ws.save_marks(stem, merged)
    return json.dumps({"added": len(merged) - len(old), "marks": merged}, ensure_ascii=False)


@mcp.tool()
def export(src_dir: str, stems: list = None) -> str:
    """导出匿名图（自动扫描 @提及/灰字引用标题；导出前用户编号按 图片序号→图内位置 重排）。
    stems=null 时导出全部已标注图"""
    ws = _ws(src_dir)
    renumber_n = core.renumber_by_position(ws)
    targets = stems if stems else [os.path.splitext(f)[0] for f in ws.list_images()]
    out = []
    for stem in targets:
        marks = ws.load_marks(stem)
        if not marks:
            continue
        _, st = core.export_image(ws, stem, marks)
        out.append({"stem": stem, **st})
    ws.save_mapping()
    return json.dumps({"renumbered": renumber_n, "exported": out,
                       "users": len(ws.mapping["users"])}, ensure_ascii=False)


@mcp.tool()
def get_mapping(src_dir: str) -> str:
    """查看全局用户映射"""
    return json.dumps(_ws(src_dir).mapping, ensure_ascii=False)


@mcp.tool()
def remove_user(src_dir: str, name: str) -> str:
    """从全局映射删除误建用户（如把 UI 按钮文字当成了用户名）"""
    ok = core.mapping_remove(_ws(src_dir), name)
    return f"removed {name}: {ok}"


if __name__ == "__main__":
    mcp.run()
