# -*- coding: utf-8 -*-
"""给 site-packages 里的 paddlex 打 modelscope 惰性导入补丁。

背景：paddlex 的 inference/utils/official_models.py 在模块顶层 `import modelscope`，
而 modelscope 被导入时会把 torch 拉进当前进程，其自带的 cudnn DLL 与 paddlepaddle-gpu
冲突（Windows 进程内同名 DLL 只能共存一份），OCR 进程因此报 cudnn 错误。补丁内容：
删除顶层 `import modelscope`（原位留 NOTE 注释），并在文件内首个实际使用 modelscope
的位置前插入同缩进的惰性导入。

补丁直接改在安装产物上，每次升级/重装 paddlex 都会被覆盖，届时重跑本脚本即可；
重复运行自动跳过。用法：python patch_paddlex.py [official_models.py 路径]
（省略参数时自动定位当前 Python 环境里的 paddlex）
"""
import re
import sys
from pathlib import Path

MARKER = "故改为使用处懒加载"
NOTE = (
    "# NOTE: `import modelscope` 在模块顶层会把 torch 拉进当前进程，其自带 cudnn DLL 会与\n"
    "# paddlepaddle-gpu 的 cudnn 冲突（Windows 进程内同名 DLL 只能共存一份）。modelscope\n"
    "# 仅在模型下载时才真正用到，模型已缓存时无需加载，故改为使用处懒加载。"
)
USE = re.compile(r"^(\s+)(?:modelscope\.|import modelscope\b)")


def locate():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    import importlib.util
    spec = importlib.util.find_spec("paddlex")
    if not spec or not spec.submodule_search_locations:
        sys.exit("未找到 paddlex：请先安装，或把 official_models.py 路径作为参数传入")
    return Path(next(iter(spec.submodule_search_locations))) / "inference/utils/official_models.py"


def main():
    path = locate()
    if not path.exists():
        sys.exit(f"文件不存在：{path}")
    src = path.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"补丁已生效，跳过：{path}")
        return
    nl = "\r\n" if "\r\n" in src else "\n"
    out, top_done, lazy_done = [], False, False
    for ln in src.splitlines(keepends=True):
        if re.match(r"^import modelscope\b", ln):
            out.append(NOTE.replace("\n", nl) + nl)  # 顶层导入原位换成说明注释
            top_done = True
            continue
        if not lazy_done:
            m = USE.match(ln)
            if m:  # 首个使用处前插入同缩进的惰性导入
                out.append(m.group(1) + "import modelscope  # 延迟导入：见文件顶部注释" + nl)
                lazy_done = True
        out.append(ln)
    if not (top_done and lazy_done):
        sys.exit(f"未找到预期的顶层 `import modelscope` 或其使用处，paddlex 新版结构可能有变，请人工核对：{path}")
    bak = path.with_suffix(path.suffix + ".anon-bak")
    if not bak.exists():
        bak.write_text(src, encoding="utf-8")
    path.write_text("".join(out), encoding="utf-8")
    print(f"补丁完成：{path}\n原文件备份于：{bak}")


if __name__ == "__main__":
    main()
