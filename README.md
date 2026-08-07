# vision-bridge-for-codex-minimax

Codex 多模态桥接技能：图片/视频理解 + 文生图，基于 MiniMax 官方 API（OpenAI 兼容 Chat Completions + Files API + image_generation）。

## 功能

- 图片理解：本地路径 / URL / data URL，支持 JPEG、PNG、GIF、WEBP；超过 10MB 的图片自动压缩（优先 Pillow，降级 ffmpeg）。
- 视频理解：本地路径 / URL / `mm_file://{file_id}`，支持 MP4、AVI、MOV、MKV；超过 45MB 自动走 Files API 上传（purpose=`video_understanding`，最大 512MB，保存 7 天）；超过 512MB 自动用 ffmpeg 压缩；远程视频超过 50MB 自动下载后上传；长视频未指定 fps 时自动降帧率控制 token。
- 文生图：MiniMax `image-01` / `image-01-live`，支持宽高比、生成数量、seed、画风（`--style`）、prompt 优化、水印、下载到本地。
- 自动降级：MiniMax-M3 失败（限流 / 网络 / Key 失效）自动切换到 GLM-4.1V（图片场景）。
- 纯标准库实现（Python 3.8+），无第三方依赖；自动压缩/转码可选依赖 Pillow 或 ffmpeg。

## 安装

1. 将本仓库内容放入 Codex 技能目录：

   - Windows：`C:\Users\<你的用户名>\.codex\skills\vision-bridge\`
   - macOS / Linux：`~/.codex/skills/vision-bridge/`

2. 配置 API Key（二选一）：

   - 复制 `scripts/vision_config.example.json` 为 `scripts/vision_config.json`，填入 MiniMax 与智谱的 API Key；
   - 或设置环境变量 `MINIMAX_API_KEY` 和 `ZHIPU_API_KEY`。

## 用法

```powershell
# 图片理解
python vision.py 图片.png -p "描述这张图片"

# 视频理解（本地小视频自动转 base64，大视频自动上传 Files API）
python vision.py 视频.mp4 -p "总结视频内容"

# 远程/已上传视频
python vision.py --video https://example.com/a.mp4 -p "识别视频"
python vision.py --video mm_file://file_id -p "分析视频"

# 文生图
python vision.py --generate "一只橘猫坐在窗台上，黄昏光线，电影感"
python vision.py --generate "..." --gen-model image-01-live --style 水彩 --aspect-ratio 16:9 --count 2 --save-dir ./outputs
```

常用参数：

- `--detail low|default|high`：图片/视频细节级别
- `--fps 0.2-5`：视频抽帧帧率（默认 1）
- `--max-long-side-pixel <像素>`：限制最长边
- `--upload`：本地视频强制走 Files API
- `--provider glm`：强制使用 GLM
- `--json`：输出完整原始响应
- 文生图：`--gen-model image-01|image-01-live`、`--aspect-ratio 1:1|16:9|4:3|3:2|2:3|3:4|9:16|21:9`、`--count 1-9`、`--width/--height`（仅 image-01）、`--style`（仅 image-01-live）、`--seed`、`--prompt-optimizer`、`--watermark`、`--save-dir <目录>`

## 配置

`scripts/vision_config.json` 结构：

```json
{
  "primary": {
    "provider": "minimax",
    "base_url": "https://api.minimaxi.com/v1",
    "api_key": "你的 MiniMax API Key",
    "model": "MiniMax-M3"
  },
  "fallback": {
    "provider": "glm",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "你的智谱 API Key",
    "model": "glm-4.1v-thinking-flash"
  }
}
```

## 安全说明

- `scripts/vision_config.json` 已被 `.gitignore` 忽略，请勿提交真实 API Key。
- 官方接口限制：图片 ≤10MB；视频 URL/base64 ≤50MB（请求体 ≤64MB），大视频走 Files API ≤512MB；文生图 prompt ≤1500 字符，生成 URL 24 小时有效。
- MiniMax-M3 当前不支持音频输入。
