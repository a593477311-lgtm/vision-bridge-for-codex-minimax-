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
- 问题缺省时从 stdin 读取：`echo "请描述画面" | python vision.py 图片.png`。
- 可选参数：`--detail low|default|high`（默认 default）、`--fps 0.2-5`（视频抽帧，默认 1）、`--max-long-side-pixel <像素>`、`--upload`（本地视频强制走 Files API）、`--thinking`、`--provider glm`、`--json`、`--model <名>`。

## 注意

- 官方格式与限制（MiniMax-M3）：图片支持 JPEG/PNG/GIF/WEBP，≤10MB；视频支持 MP4/AVI/MOV/MKV，URL 或 base64 ≤50MB（请求体上限 64MB）；更大的视频走 Files API 上传（purpose=`video_understanding`，≤512MB，保存 7 天）并转为 `mm_file://{file_id}` 引用。
- 自动处理（尽量不报错）：本地/远程图片 >10MB 自动压缩（优先 Pillow，降级 ffmpeg）；本地视频 >45MB 自动上传 Files API，>512MB 自动用 ffmpeg 压缩后再上传；远程视频 >50MB 自动下载后上传 Files API；长视频未指定 `--fps` 时自动降 fps（>5 分钟用 0.5，>30 分钟用 0.2）；请求体 >64MB 自动以更小图片体积重试一次。
- 文生图：prompt ≤1500 字符；仅支持 MiniMax（image-01 / image-01-live）；生成的 URL 24 小时有效，需要本地文件时用 `--save-dir`；`--style` 仅 image-01-live 生效（漫画/元气/中世纪/水彩等画风，以官方文档为准）；21:9 与 `--width/--height` 仅 image-01。
- 音频暂不支持：MiniMax-M3 官方文档明确当前不支持音频输入。
- 输出末尾会标注实际提供商（`[提供商] minimax (MiniMax-M3)` 或 `glm (...)`）；MiniMax 失败（限流/网络/Key 失效）会自动降级 GLM（图片场景），无需重试。
- API Key 与模型配置在 `scripts\vision_config.json`；环境变量 `MINIMAX_API_KEY` / `ZHIPU_API_KEY` 可覆盖。
- 需要本机可用的 `python` 命令（3.8+，纯标准库无依赖）；自动压缩/转码依赖 Pillow 或 ffmpeg（有其一即可），长视频降 fps 依赖 ffprobe，缺失时会自动跳过对应优化。
