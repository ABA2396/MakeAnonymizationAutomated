# 截图匿名化工具

**优先用 B 站油猴脚本**：在网页上直接打码，头像、名字、@提及一次到位。已存成图片的
内容（QQ 聊天记录、网页截图等）用本地半自动工具：人工（或 AI）点选/框选标注，全局
映射保证同一用户名跨图同色、同 ｢用户首字母｣ 标签（内部编号也一致），导出时自动扫描
@提及 与灰字引用标题一并打码；草稿核对与成图验收推荐调视觉模型读 compare 对比图
完成（见 MCP 一节的工作流）。

## B 站油猴脚本（bilibili_anon.user.js，优先）

网页端直接打码，覆盖 视频(`/video/*`)、动态(`/opus/*`)、专栏(`/read/*`) 三类页面：

1. Tampermonkey 新建脚本，粘贴本文件全部内容保存（或把 .user.js 拖进 Tampermonkey 图标安装）
2. 页面左下角 ｢码｣ 按钮或 Alt+M 进入打码编辑态（Esc/Alt+M 或再点退出）
3. **编辑态禁用页面一切跳转与点击动作**（链接 href 被临时移除，退出时恢复）——翻页、
   展开回复等页面操作须先退出编辑态再做
4. 编辑态：点头像 = 盖纯色圆；点用户名/@提及 = 替换为 ｢用户首字母｣（名字按该用户
   颜色显示，与头像圆同色）并自动补盖同楼层头像；点已打码元素 = 撤销该处（该用户
   不再被自动重打，页面已无其打码点时连映射一起清除；工具条 ｢应用已知名单｣ 不受此限）
5. 工具条：
   - ｢全部打码｣：本页所有评论用户一键打码，跳过上方排除名单；翻页后对新增楼层再点一次
   - 排除名单输入框：逗号/空格分隔，默认 MAA-Official
   - ｢应用已知名单｣：当前页所有已认识的用户名一键打码
   - ｢自动应用｣：开启后（默认开）页面新加载的内容里出现已知名自动打码，无需手动
   - ｢导出映射｣：把本次会话的映射以 anonymizer 桌面工具 mapping.json 的
     `{"users": {名字: {uid, letter, rgb, hue}}}` 格式复制到剪贴板，可并入桌面工具使用
   - ｢撤销本页全部打码｣：恢复本页原样并清空映射

同一用户（space.bilibili.com 的 mid）在本页恒同字母、同颜色；映射只存内存，
**仅本次页面生效**：刷新/跳转即清空，不写 localStorage 或任何持久化存储；
编号按本次会话内首次打码顺序递增。

## 本地半自动工具（处理截图 / QQ 消息等图片）

### 图形界面（人工标注）

```
python ui.py <图片目录>
```

也可双击同目录的 ｢标注工具.bat｣（启动后点 ｢打开目录｣ 选择），或把图片文件夹
直接拖到 bat 文件上打开。

| 操作 | 效果 |
| --- | --- |
| 左键单击 | 吸附头像圆（局部连通域，失败退霍夫圆），OCR 自动预填名字供审核 |
| 左键拖拽 | 框选用户名/提及文本覆盖框，OCR 自动预填名字供审核 |
| 中键拖拽 | 平移视图 |
| Alt+滚轮 / 滚轮 | 缩放 / 滚动 |
| 双击标注 | 修改名字 |
| 拖动已有文字框 | 平移该覆盖框（微调位置），Ctrl+Z 可撤销 |
| 右键单击 | 按点击位置拆分删除：点在文字框上只删框（保留头像圆），点在圆上只删圆（保留文字框）；其余删除选中的整条 |
| 右键拖拽 | 框选头像圆（框中心为圆心、长边为直径），修正自动检测过小/偏移的圆；框内有旧圆时只替换圆 |
| Ctrl+Z | 撤销（快照制：标注/删除/改名/草稿生成与采纳都可逐步回退，仅限本次会话） |
| Ctrl+S / E / A | 保存标注 / 导出当前 / 全部导出 |
| D / T | 生成自动草稿 / 采纳全部草稿 |

名字输入留空直接确定 = 纯涂色（不进映射）；选 ｢MAA-Official｣ = 放弃本次标注。
同一名字第二次出现时直接在下拉里选，颜色与字母自动一致。
顶部 ｢排除｣ 输入框（逗号分隔，默认 MAA-Official）里的名字不打码、不进映射，
写回 mapping.json 的 exclude 字段。
导出的名字标签按该用户专属颜色显示（与头像圆同色）。内部编号不按标注先后：
每次导出前自动按 图片序号 → 图内从上到下 重排（颜色与字母保持不变，草稿不参与编号）。
OCR 模型在打开目录时即后台加载（首次几十秒，进程内只加载一次）；自动草稿与导出在
后台线程执行，期间界面保持响应、标注输入被忽略。

### Python 调用（无 UI 依赖）

```python
import sys; sys.path.insert(0, r"<anonymizer 目录>")
import core
ws = core.Workspace(r"<图片目录>")
av = core.snap_avatar(img, x, y)         # 点击 → 头像圆
marks = [{"avatar": [cx, cy, r], "name_box": [x1, y1, x2, y2], "name": "某人"}]
ws.save_marks("1", marks)
core.export_image(ws, "1", marks)        # 出 output/1.png + compare/1.png
```

### MCP server（AI 编程调用）

依赖 `pip install mcp`。任意 MCP 客户端（ZCode / Claude Desktop / Cursor 等）的
`mcpServers` 配置，路径按 anonymizer 目录的实际存放位置填写：

```json
"anonymizer": {
  "command": "python",
  "args": ["<anonymizer 目录>/mcp_server.py"]
}
```

工具：`list_images` / `get_marks` / `set_marks` / `snap_avatar` / `auto_draft` /
`export` / `get_mapping` / `remove_user`。
AI 建议流程：`auto_draft` 出底稿 → 读 compare 图视觉核对（删误检、补漏检）→
`set_marks` 保存修正 → `export` → 再读 compare 图验收。

## 依赖

Python 3.12（`tkinter` 需勾选，官方安装器默认含）。通用部分 `pip install -r requirements.txt`：

| 包 | 版本 | 用途 |
| --- | --- | --- |
| numpy | 1.26.4 | 图像数组 |
| opencv-python | 4.10.0 | 连通域/霍夫圆、编解码（含中文路径处理） |
| pillow | 10.4.0 | 绘制（圆/文字）、tkinter 显示 |
| pypinyin | 0.55.0 | 中文首字母（非中英名字由 core 内 FNV-1a 哈希成 A–Z，与油猴脚本一致） |
| paddleocr | 3.2.0 | OCR（传递依赖 paddlex 3.2.1） |
| mcp | 1.23.3 | 仅 mcp_server.py 需要，只用 UI 可不装 |

Paddle 推理后端二选一：

- **GPU**（PyPI 无 Windows GPU wheel，必须官方源）：
  `pip install paddlepaddle-gpu==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/`
  实测 CUDA 12.6 + cudnn 9.5 可用。core.py 开头的 torch 预导入是给同进程还要用
  torch 的场景留的（torch 非本工具依赖，没有则无影响），勿删。
- **CPU**：`pip install paddlepaddle==3.2.0`，速度慢但零配置；环境变量
  `ANON_OCR_DEVICE=cpu` 可强制 CPU。

升级注意：paddlex 的 `paddlex/inference/utils/official_models.py` 在模块顶层
`import modelscope`，而 modelscope 被导入时会把 torch 拉进 OCR 进程，其自带 cudnn DLL
与 paddlepaddle-gpu 冲突（Windows 进程内同名 DLL 只能共存一份）。改法：删掉该顶层
导入（原位留注释说明），在文件内首个实际使用 modelscope 的位置前补同缩进的
`import modelscope`（惰性导入，模型已缓存时不再加载 torch）。本目录的
`patch_paddlex.py` 自动完成上述改动（幂等，原文件留 `.anon-bak` 备份），**每次
升级/重装 paddlex 后重跑一次**：

```
python patch_paddlex.py
```

已打补丁会提示跳过；若提示 ｢结构可能有变｣ 说明新版该文件改了形态，需人工核对。
OCR 模型首次运行自动下载到 `~/.paddlex/official_models/`。

## 文件布局

- `core.py` 核心库：吸附、映射、打码导出、OCR（切块+落盘缓存，GPU 默认，
  `ANON_OCR_DEVICE=cpu` 可切；模型进程内单例，全进程只加载一次）
- `ui.py` tkinter 标注界面
- `mcp_server.py` MCP stdio server
- `patch_paddlex.py` 升级 paddlex 后重打 modelscope 惰性导入补丁（背景与用法见 ｢依赖｣）
- `bilibili_anon.user.js` B 站油猴脚本
- 产物：`<图片目录>/output/`（匿名图、`compare/` 对比图、`marks/` 标注 JSON、
  `mapping.json` 全局映射（含 exclude 排除名单）、`.ocr_cache/`）；每次保存标注时
  上一版自动留 `marks/<名>.json.bak`，改崩了可手动改回
