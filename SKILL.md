---
name: vision-bridge
description: Multimodal vision bridge for Codex conversations. Use whenever the user shares, attaches, pastes, or references an image, screenshot, photo, scan, diagram, chart, meme, image URL, video, or video URL and asks to describe, analyze, OCR / extract text, compare, summarize, or answer questions about it, when any task requires visual understanding of a local image or video file that Codex cannot see directly, or when the user asks to generate or create an image from a text description. Delegates multimodal understanding to MiniMax-M3 (primary, supports images and videos) with automatic fallback to GLM-4.1V-Thinking-Flash for images, uses MiniMax image-01 / image-01-live for text-to-image generation, and returns text analysis into the main conversation which stays with Codex.
---

# Vision Bridge（多模态识图/识视频/文生图）

主对话继续由 Codex 负责；本技能负责把多模态输入转成文字分析、以及用文本生成图片，结果回到主对话使用。

## 触发时机

- 用户提供、粘贴或引用本地图片、截图、照片、扫描件、图表、表情包、视频等，要求描述、分析、OCR、对比、总结或提问。
- 任务需要理解图片或视频内容，而当前模型无法直接“看见”。
- 用户要求根据文字描述生成图片（文生图）。

## 用法

```powershell
python "C:\Users\gg1\.codex\skills\vision-bridge\scripts\vision.py" "<图片或视频路径>" -p "<问题>"
```

- 图片（本地或 http(s)/data URL，可多个）：`python vision.py 图1.png 图2.png -p "对比这两张图"`。
- 视频（本地路径、URL 或 `mm_file://{file_id}`）：`python vision.py 视频.mp4 -p "描述视频内容"`；本地小视频自动转 base64，大视频自动上传 Files API。
- 已上传的大视频引用：`python vision.py --video mm_file://file_id -p "分析视频"`。
- 远程视频：`python vision.py --video https://example.com/a.mp4 -p "识别视频"`。
- 文件（仅 URL / `mm_file://`，非官方保证能力）：`python vision.py --file https://example.com/doc.pdf -p "总结"`。
- 文生图：`python vision.py --generate "一只橘猫坐在窗台上，黄昏光线，电影感"`，默认只输出图片 URL（24 小时有效）。
- 常用生图参数：`--gen-model image-01|image-01-live`、`--aspect-ratio 1:1|16:9|4:3|3:2|2:3|3:4|9:16|21:9`、`--count 1-9`、`--width/--height`（仅 image-01，512-2048 且为 8 的倍数）、`--style`（仅 live）、`--seed`、`--prompt-optimizer`、`--watermark`、`--save-dir <目录>`（下载到本地，方便直接展示）。
- 问题缺省时从 stdin 读取；stdin 无输入时自动使用默认描述提示词，不会卡住：`echo "请描述画面" | python vision.py 图片.png`。
- 可选参数：`--detail low|default|high`（默认 default）、`--fps 0.2-5`（视频抽帧，默认 1）、`--max-long-side-pixel <像素>`、`--upload`（本地视频强制走 Files API）、`--thinking`、`--provider glm`、`--json`、`--model <名>`、`--max-tokens <数量>`（限制回答长度，批量检查推荐 300-900）、`--concise`（简洁中文回答：≤200 字、不用 emoji/表格，默认 max-tokens=400）、`--for-llm`（面向无视觉模型的结构化抽取；自动使用 max-tokens 2400 与 detail high，见下方“DeepSeek × MiniMax 协作”）。
- 超时：脚本默认 `--timeout 180` 秒；大文件上传或高细节分析建议调用方把 shell 的 timeout_ms 设为 `--timeout + 60` 以上（如 300000）。

## DeepSeek × MiniMax 协作（无视觉主模型）

当主对话模型不支持视觉（如 DeepSeek）时，用“两段式”提示词把 MiniMax 变成它的眼睛：

1. **MiniMax 提取层（结构化描述）**：加 `--for-llm`，强制输出逐字文字、**空间关系（元素相对位置/层级/百分比坐标）**、**颜色与状态语义（报错红/成功绿/警告黄/语法高亮/主题）**、数值、以及“可观察事实 / 推断”分离的结构化描述：

   `python vision.py 图片.png --for-llm -p "用户原始问题（可选，会让提取更聚焦）" --detail high`

2. **DeepSeek 消费层**：把上面输出的描述原样交给 DeepSeek，并在其系统提示词中声明：

   ```text
   你无法直接查看图片。下面“图片描述”来自视觉模型 MiniMax，是你唯一的视觉信息来源。
   规则：
   - 只依据这段描述回答，不要假设自己“看到”了图片；
   - 描述中标“推断/无法确定”的内容，回答时要明确转述为不确定；
   - 若描述缺少回答所需信息，直接说“需要重新提取图片信息”，不要编造。
   ```

模板要点（已内置到 `--for-llm`）：

- 保留 1-7 全部小节骨架，即使问题只关注某一方面也不整节缺失；
- 文字逐字转写并区分“标题/按钮/占位符/输入值”，不确定字符标 `[存疑]`，禁止猜词；
- 空间关系给相对位置、层级、百分比坐标（标注估算），并给出图片尺寸；
- 颜色给语义（报错红/成功绿/警告黄/语法高亮/主题）而非只列色值；
- 表格/列表输出结构化映射（列名→值 或 JSON）；
- 疑似推断（焦点、选中、当前行、展开态）必须放进“推断”区，不能混入可观察事实。

代码开发场景示例（前后端同学常把截图直接交给 DeepSeek）：

- 前端页面/后台截图（布局、组件状态、颜色语义、弹窗位置）：
  `python vision.py 页面.png --for-llm -p "页面有什么报错？错误提示在什么位置？删除弹窗相对页面的位置？布局和配色如何？" --detail high`
- IDE/代码截图（文件树、活动文件、行号、错误标记、语法高亮）：
  `python vision.py ide.png --for-llm -p "代码报了什么错？在哪个文件哪一行？终端在编辑器什么位置？高亮颜色代表什么？" --detail high`
- 终端/后端报错截图（命令、退出码、Traceback 位置、颜色状态）：
  `python vision.py 终端.png --for-llm -p "后端启动失败原因是什么？退出码是多少？错误输出在终端什么位置？" --detail high`

## 常用场景

- 截图 OCR / 全量识别（文件列表、命令、界面元素逐项解释）：
  `python vision.py 截图.png -p "请完整识别截图中的全部文字和内容，并逐个解释每一项是什么、有什么作用，用中文回答。" --detail high`
- 界面描述（软件/网页/报错/会话列表）：
  `python vision.py 截图.png -p "这是什么软件界面？有哪些窗口/面板/文字/会话列表/按钮？特别注意报错信息和当前状态。"`
- 批量质检（水印/乱码/构图/留白，逐张回答）：
  `python vision.py 图1.jpg 图2.jpg 图3.jpg --concise -p "逐张检查：1) 是否干净无乱码；2) 是否含文字/数字/水印；3) 内容大致是什么。格式：编号: 结论。"`
- 多图对比：
  `python vision.py a.png b.png -p "对比这两张图：共同点和差异，逐项说明。"`
- 文生图并保存本地（方便直接展示）：
  `python vision.py --generate "一只橘猫坐在窗台上，黄昏光线，电影感" --gen-model image-01-live --style "水彩" --aspect-ratio 16:9 --save-dir outputs`

## 常见问题

- Windows 终端 GBK 乱码 / UnicodeEncodeError：本脚本已自动把 stdout/stderr 切到 UTF-8；若调用旧脚本或第三方脚本，先执行 `$env:PYTHONIOENCODING='utf-8'`。
- HTTP 429 限流：已自动指数退避重试（最多 3 次）并支持 `Retry-After`；仍失败会自动降级 GLM（图片场景）。
- 视频被当成图片（报 `image data url media type "video/mp4" not supported`）：本地路径与 URL 已按扩展名自动分流，带查询参数的视频 URL 也已兼容；仍异常时显式用 `--video <路径或URL>`。
- HTTP 422 / `sensitive`：MiniMax 内容安全过滤，调整图片或描述后重试，或改用 `--provider glm`。
- 请求超时：调大 `--timeout`（如 300），并让调用方 timeout_ms 更大。
- 视频输入降级 GLM 大概率失败：GLM 可能不支持视频，脚本会给出警告并如实报错，不要误以为是路径问题。
- 输出已自动剥离 MiniMax/GLM 的 `<think>...</think>` 思考内容（包括 `--json`、`--for-llm` 和截断的未闭合标签）；若上游再次泄漏，先检查是否调用了活跃目录 `scripts\vision.py`、再用 `--json` 查看清理后的响应并确认响应字段中没有 `reasoning_content`，必要时保留脱敏响应和复现命令排查。
- API Key 明文存放在 `scripts\vision_config.json`：建议改用环境变量 `MINIMAX_API_KEY` / `ZHIPU_API_KEY` 覆盖，避免配置文件外泄。

## 注意

- 官方格式与限制（MiniMax-M3）：图片支持 JPEG/PNG/GIF/WEBP，≤10MB；视频支持 MP4/AVI/MOV/MKV，URL 或 base64 ≤50MB（请求体上限 64MB）；更大的视频走 Files API 上传（purpose=`video_understanding`，≤512MB，保存 7 天）并转为 `mm_file://{file_id}` 引用。
- 自动处理（尽量不报错）：本地/远程图片 >10MB 自动压缩（优先 Pillow，降级 ffmpeg）；本地视频 >45MB 自动上传 Files API，>512MB 自动用 ffmpeg 压缩后再上传；远程视频 >50MB 自动下载后上传 Files API；长视频未指定 `--fps` 时自动降 fps（>5 分钟用 0.5，>30 分钟用 0.2）；请求体 >64MB 自动以更小图片体积重试一次。
- 文生图：prompt ≤1500 字符；仅支持 MiniMax（image-01 / image-01-live）；生成的 URL 24 小时有效，需要本地文件时用 `--save-dir`；`--style` 仅 image-01-live 生效（漫画/元气/中世纪/水彩等画风，以官方文档为准）；21:9 与 `--width/--height` 仅 image-01。
- 音频暂不支持：MiniMax-M3 官方文档明确当前不支持音频输入。
- 输出末尾会标注实际提供商（`[提供商] minimax (MiniMax-M3)` 或 `glm (...)`）；MiniMax 失败（限流/网络/Key 失效）会自动降级 GLM（图片场景），无需重试。
- API Key 与模型配置在 `scripts\vision_config.json`；环境变量 `MINIMAX_API_KEY` / `ZHIPU_API_KEY` 可覆盖。
- 需要本机可用的 `python` 命令（3.8+，纯标准库无依赖）；自动压缩/转码依赖 Pillow 或 ffmpeg（有其一即可），长视频降 fps 依赖 ffprobe，缺失时会自动跳过对应优化。
