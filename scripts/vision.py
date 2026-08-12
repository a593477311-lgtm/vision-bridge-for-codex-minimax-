#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多模态分析入口：MiniMax-M3 优先识图/视频，GLM 视觉模型自动降级备用。
主对话仍由 Codex 负责，本工具只负责把图片/视频/文件转成文字分析结果。

支持输入：
- 图片：本地路径 / http(s) URL / data URL，官方支持 JPEG/PNG/GIF/WEBP，≤10MB
- 视频：本地路径 / http(s) URL / data URL / mm_file://{file_id}
  官方支持 MP4/AVI/MOV/MKV，URL 或 base64 ≤50MB，请求体 ≤64MB
- 文件：仅接受 http(s) URL 或 mm_file://{file_id}（M3 官方仅保证图片/视频输入）

自动处理（尽量不报错）：
- 本地/远程图片 >10MB：自动压缩后发送（优先 Pillow，降级 ffmpeg）
- 本地视频 >45MB：自动走 Files API 上传（purpose=video_understanding，≤512MB，保存 7 天）
- 本地视频 >512MB：自动用 ffmpeg 压缩后再上传（无 ffmpeg 才报错）
- 远程视频 >50MB：自动下载后上传 Files API
- 长视频未指定 --fps：自动降 fps（>5 分钟用 0.5，>30 分钟用 0.2）
- 请求体 >64MB：自动以更小压缩比重试一次

用法示例：
  python vision.py 图片.png -p "这张图里有什么？"
  python vision.py 图1.png 图2.png -p "对比这两张图"
  python vision.py 视频.mp4 -p "描述视频内容" --fps 2 --detail high
  python vision.py 大视频.mp4 -p "总结视频"            # >45MB 自动上传 Files API
  python vision.py --video https://example.com/a.mp4 -p "识别视频"
  python vision.py --video mm_file://file_id -p "分析已上传视频"
  python vision.py https://example.com/a.png -p "识别图中文字"
  echo "请描述画面" | python vision.py 图片.png
  python vision.py 图片.png -p "..." --thinking     # 开启深度思考
  python vision.py 图片.png -p "..." --json          # 输出完整原始 JSON
  python vision.py 图片.png -p "..." --provider glm  # 强制指定提供商
  python vision.py 图片.png -p "..." --concise       # 简短回答（≤200字、不用 emoji/表格）
  python vision.py 图片.png -p "..." --max-tokens 400  # 限制回答长度

文生图示例：
  python vision.py --generate "一只橘猫坐在窗台上，黄昏光线，电影感"
  python vision.py --generate "..." --gen-model image-01-live --style "..." --aspect-ratio 16:9 --count 2 --save-dir ./outputs
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# 统一 UTF-8 输出，避免 Windows 终端 GBK 乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_MODEL = {
    "minimax": "MiniMax-M3",
    "glm": "glm-4.1v-thinking-flash",
}
DEFAULT_BASE_URL = {
    "minimax": "https://api.minimaxi.com/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
}
ENV_KEY = {
    "minimax": "MINIMAX_API_KEY",
    "glm": "ZHIPU_API_KEY",
}
CONFIG_PATH = Path(__file__).resolve().parent / "vision_config.json"

EXTRA_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr"}

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_URL_MAX_BYTES = 50 * 1024 * 1024
VIDEO_BASE64_MAX_BYTES = 45 * 1024 * 1024  # base64 后需小于请求体 64MB 上限
VIDEO_UPLOAD_MAX_BYTES = 512 * 1024 * 1024
REQUEST_BODY_MAX_BYTES = 64 * 1024 * 1024
IMAGE_COMPRESS_TARGET = 8 * 1024 * 1024
IMAGE_COMPRESS_TARGET_TIGHT = 1_500_000
DOWNLOAD_HARD_CAP = 600 * 1024 * 1024


def _suffix_of(item: str) -> str:
    """返回本地路径或 URL 的小写扩展名（正确处理查询参数）。"""
    if item.startswith(("http://", "https://")):
        return Path(urlparse(item).path).suffix.lower()
    if item.startswith(("data:", "mm_file://")):
        return ""
    return Path(item).suffix.lower()


class PayloadTooLargeError(RuntimeError):
    pass


def _temp_file(suffix: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(name)


def read_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception as e:
            sys.exit(f"配置文件解析失败 {CONFIG_PATH}: {e}")
    return {}


def load_providers(force: str | None, model_override: str | None, api_key_override: str | None):
    cfg = read_config()

    def make_provider(key: str):
        if key not in cfg or not cfg[key]:
            return None
        spec = cfg[key]
        name = spec.get("provider") or key
        provider = {
            "name": name,
            "base_url": spec.get("base_url") or DEFAULT_BASE_URL.get(name),
            "api_key": spec.get("api_key"),
            "model": spec.get("model") or DEFAULT_MODEL.get(name),
        }
        if not provider["base_url"] or not provider["model"]:
            sys.exit(f"提供商 {name} 缺少 base_url 或 model 配置")
        env_key = ENV_KEY.get(name)
        if env_key and os.environ.get(env_key):
            provider["api_key"] = os.environ[env_key]
        if api_key_override and key == "primary":
            provider["api_key"] = api_key_override
        if model_override and key == "primary":
            provider["model"] = model_override
        if not provider["api_key"]:
            sys.exit(f"提供商 {name} 缺少 API Key（配置 vision_config.json 或环境变量 {env_key}）")
        return provider

    primary = make_provider("primary")
    fallback = make_provider("fallback")

    if force:
        wanted = primary if primary and primary["name"] == force else fallback
        if not wanted:
            sys.exit(f"配置中没有提供商 {force}（当前：{primary and primary['name']} / {fallback and fallback['name']}）")
        return [wanted]
    return [p for p in (primary, fallback) if p]


def to_data_url(path: str) -> str:
    p = Path(path)
    if not p.exists():
        sys.exit(f"文件不存在: {path}")
    mime = mimetypes.guess_type(path)[0] or EXTRA_MIME.get(p.suffix.lower(), "application/octet-stream")
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def http_head_size(url: str, timeout: int) -> int | None:
    """返回 Content-Length；HEAD 失败或无法获取时返回 None（表示按原样直传）。"""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "vision-bridge/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else None
    except Exception:
        return None


def download_to_temp(url: str, cap_bytes: int, timeout: int) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "vision-bridge/1.0"})
    suffix = Path(urlparse(url).path).suffix or ".tmp"
    out = _temp_file(suffix)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = 0
            with open(out, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap_bytes:
                        raise RuntimeError(f"下载超过大小上限 {cap_bytes / 1024 / 1024:.0f}MB")
                    f.write(chunk)
        return out
    except Exception:
        out.unlink(missing_ok=True)
        raise


def data_url_to_temp(data_url: str) -> Path:
    header, _, b64 = data_url.partition(",")
    mime = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    ext = mimetypes.guess_extension(mime) or ".tmp"
    out = _temp_file(ext)
    try:
        out.write_bytes(base64.b64decode(b64))
        return out
    except Exception:
        out.unlink(missing_ok=True)
        raise


def compress_image_with_pillow(src: Path, max_bytes: int) -> Path:
    from PIL import Image

    img = Image.open(src)
    if getattr(img, "is_animated", False):
        img.seek(0)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    combos = [(side, quality) for side in (2048, 1600, 1280, 1024, 768, 512) for quality in (85, 75, 60, 45, 30)]
    for side, quality in combos:
        im = img.copy()
        im.thumbnail((side, side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            out = _temp_file(".jpg")
            out.write_bytes(buf.getvalue())
            return out
    raise RuntimeError("图片压缩后仍超过大小上限，请手动压缩后重试。")


def compress_image_with_ffmpeg(src: Path, max_bytes: int) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("本机没有 Pillow 和 ffmpeg，无法自动压缩大图片。")
    for scale, q in [
        ("min(2048,iw)", 5),
        ("min(1600,iw)", 7),
        ("min(1280,iw)", 8),
        ("min(1024,iw)", 10),
        ("min(768,iw)", 12),
        ("min(512,iw)", 15),
    ]:
        out = _temp_file(".jpg")
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-i", str(src), "-vf", f"scale={scale}:-2", "-q:v", str(q), str(out)],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0 and out.stat().st_size <= max_bytes:
                return out
        except Exception:
            pass
        out.unlink(missing_ok=True)
    raise RuntimeError("ffmpeg 压缩后图片仍超过大小上限，请手动压缩后重试。")


def image_to_data_url(src: Path, tight: bool) -> str:
    target = IMAGE_COMPRESS_TARGET_TIGHT if tight else IMAGE_COMPRESS_TARGET
    compressed = None
    try:
        try:
            compressed = compress_image_with_pillow(src, target)
        except Exception:
            compressed = compress_image_with_ffmpeg(src, target)
        return to_data_url(str(compressed))
    finally:
        if compressed:
            compressed.unlink(missing_ok=True)


def upload_file(provider: dict, path: str, timeout: int) -> str:
    """上传文件到 MiniMax Files API（purpose=video_understanding），返回 mm_file://{file_id}。"""
    if provider["name"] != "minimax":
        sys.exit(f"提供商 {provider['name']} 不支持 Files API 上传（仅 MiniMax 支持 video_understanding）")
    p = Path(path)
    if not p.exists():
        sys.exit(f"文件不存在: {path}")
    filename = p.name.replace('"', "")
    mime = mimetypes.guess_type(path)[0] or EXTRA_MIME.get(p.suffix.lower(), "application/octet-stream")
    boundary = "----visionbridge" + uuid.uuid4().hex
    body = bytearray()
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nvideo_understanding\r\n".encode("utf-8"))
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n".encode("utf-8"))
    body.extend(p.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    url = provider["base_url"].rstrip("/") + "/files/upload"
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Files API 上传失败 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Files API 网络错误: {e.reason}")

    base = data.get("base_resp") or {}
    if base.get("status_code") != 0:
        raise RuntimeError(f"Files API 上传失败: {json.dumps(data, ensure_ascii=False)}")
    file_id = (data.get("file") or {}).get("file_id")
    if not file_id:
        raise RuntimeError(f"Files API 响应缺少 file_id: {json.dumps(data, ensure_ascii=False)}")
    return f"mm_file://{file_id}"


def compress_video_with_ffmpeg(src: Path, timeout: int) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    for scale, crf in [(1280, 28), (854, 30), (640, 33)]:
        out = _temp_file(".mp4")
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-y", "-i", str(src),
                    "-vf", f"scale=min({scale},iw):-2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                    "-pix_fmt", "yuv420p", "-an", str(out),
                ],
                capture_output=True,
                timeout=max(timeout, 600),
            )
            if result.returncode == 0 and out.stat().st_size <= VIDEO_UPLOAD_MAX_BYTES:
                return out
        except Exception:
            pass
        out.unlink(missing_ok=True)
    return None


def probe_duration(path: str) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def auto_fps_for(item: str) -> float | None:
    if item.startswith(("http://", "https://", "data:", "mm_file://")):
        return None
    duration = probe_duration(item)
    if duration is None:
        return None
    if duration > 1800:
        return 0.2
    if duration > 300:
        return 0.5
    return None


def prepare_image(item: str, detail: str | None, max_long_side_pixel: int | None, tight: bool) -> dict:
    p = Path(item)
    if _suffix_of(item) in AUDIO_EXTS:
        sys.exit("音频输入暂不支持：MiniMax-M3 官方文档明确当前不支持音频输入。")

    temp_files = []
    try:
        if item.startswith(("http://", "https://")):
            size = http_head_size(item, 60)
            if size is not None and size > IMAGE_MAX_BYTES:
                print(f"[自动] 远程图片 {size / 1024 / 1024:.1f}MB 超过 10MB，下载压缩后发送...", file=sys.stderr)
                src = download_to_temp(item, DOWNLOAD_HARD_CAP, 120)
                temp_files.append(src)
                url = image_to_data_url(src, tight)
            else:
                url = item
        elif item.startswith("data:"):
            b64 = item.partition(",")[2]
            approx_size = len(b64) * 3 // 4
            if approx_size > IMAGE_MAX_BYTES:
                print(f"[自动] data URL 图片约 {approx_size / 1024 / 1024:.1f}MB，解码压缩后发送...", file=sys.stderr)
                src = data_url_to_temp(item)
                temp_files.append(src)
                url = image_to_data_url(src, tight)
            else:
                url = item
        else:
            if not p.exists():
                sys.exit(f"文件不存在: {item}")
            if p.stat().st_size > IMAGE_MAX_BYTES:
                print(f"[自动] 图片 {p.stat().st_size / 1024 / 1024:.1f}MB 超过 10MB，自动压缩后发送...", file=sys.stderr)
                url = image_to_data_url(p, tight)
            else:
                url = to_data_url(item)
    finally:
        for t in temp_files:
            t.unlink(missing_ok=True)

    block = {"type": "image_url", "image_url": {"url": url}}
    if detail:
        block["image_url"]["detail"] = detail
    elif tight:
        block["image_url"]["detail"] = "low"
    if max_long_side_pixel:
        block["image_url"]["max_long_side_pixel"] = max_long_side_pixel
    return block


def prepare_video(
    provider: dict,
    item: str,
    force_upload: bool,
    timeout: int,
    upload_cache: dict,
) -> str:
    """返回可用的视频 URL（http(s)/data/mm_file://）。"""
    if item.startswith(("mm_file://", "data:")):
        return item

    if item.startswith(("http://", "https://")):
        size = http_head_size(item, 60)
        if size is None or size <= VIDEO_URL_MAX_BYTES:
            return item
        if size > VIDEO_UPLOAD_MAX_BYTES:
            sys.exit(f"远程视频 {size / 1024 / 1024:.1f}MB 超过 512MB 上限，请先自行压缩。")
        if provider["name"] != "minimax":
            sys.exit(f"远程视频超过 50MB URL 上限，需要 Files API 上传，但 {provider['name']} 不支持；请改用 MiniMax。")
        print(f"[自动] 远程视频 {size / 1024 / 1024:.1f}MB 超过 50MB URL 上限，下载后上传 Files API...", file=sys.stderr)
        src = download_to_temp(item, VIDEO_UPLOAD_MAX_BYTES, max(timeout, 300))
        try:
            return upload_file(provider, str(src), timeout)
        finally:
            src.unlink(missing_ok=True)

    p = Path(item)
    if not p.exists():
        sys.exit(f"文件不存在: {item}")
    ext = _suffix_of(item)
    if ext in AUDIO_EXTS:
        sys.exit("音频输入暂不支持：MiniMax-M3 官方文档明确当前不支持音频输入。")
    if ext not in VIDEO_EXTS:
        print(f"[警告] {ext or '未知扩展名'} 不在视频支持列表（MP4/AVI/MOV/MKV），API 可能拒绝", file=sys.stderr)
    size = p.stat().st_size

    key = (provider["name"], str(p.resolve()))
    if key in upload_cache:
        return upload_cache[key]

    if size > VIDEO_UPLOAD_MAX_BYTES:
        print(f"[自动] 视频 {size / 1024 / 1024:.1f}MB 超过 512MB，尝试用 ffmpeg 压缩后再上传...", file=sys.stderr)
        compressed = compress_video_with_ffmpeg(p, timeout)
        if compressed is None:
            sys.exit("视频超过 512MB 且本机没有 ffmpeg（或压缩失败），请先自行压缩后重试。")
        try:
            if provider["name"] != "minimax":
                sys.exit(f"{provider['name']} 不支持 Files API 上传；请改用 MiniMax。")
            url = upload_file(provider, str(compressed), timeout)
            upload_cache[key] = url
            return url
        finally:
            compressed.unlink(missing_ok=True)

    if force_upload or size > VIDEO_BASE64_MAX_BYTES:
        if provider["name"] != "minimax":
            sys.exit(f"视频 {item} 超过 base64 上限（45MB），但 {provider['name']} 不支持 Files API 上传；请改用 MiniMax 或压缩视频。")
        print(f"[自动] 视频 {item}（{size / 1024 / 1024:.1f}MB）正在上传 Files API...", file=sys.stderr)
        url = upload_file(provider, item, timeout)
        upload_cache[key] = url
        return url

    url = to_data_url(item)
    upload_cache[key] = url
    return url


def build_content(
    provider: dict,
    images,
    videos,
    files,
    prompt: str,
    detail: str | None,
    fps: float | None,
    max_long_side_pixel: int | None,
    force_upload: bool,
    upload_cache: dict,
    timeout: int,
    tight: bool = False,
) -> list:
    content = []
    for item in images:
        content.append(prepare_image(item, detail, max_long_side_pixel, tight))
    for item in videos:
        url = prepare_video(provider, item, force_upload, timeout, upload_cache)
        block = {"type": "video_url", "video_url": {"url": url}}
        effective_detail = detail if detail else ("low" if tight else None)
        if effective_detail:
            block["video_url"]["detail"] = effective_detail
        effective_fps = fps
        if effective_fps is None:
            effective_fps = auto_fps_for(item)
            if effective_fps is not None:
                print(f"[自动] 视频时长较长且未指定 fps，自动使用 fps={effective_fps} 控制 token 用量", file=sys.stderr)
        if effective_fps:
            block["video_url"]["fps"] = effective_fps
        if max_long_side_pixel:
            block["video_url"]["max_long_side_pixel"] = max_long_side_pixel
        content.append(block)
    for item in files:
        if not item.startswith(("http://", "https://", "mm_file://")):
            sys.exit(
                f"--file 仅支持 http(s) URL 或 mm_file://{'{file_id}'}：{item}。"
                "M3 官方只保证图片/视频输入；本地文档请先转成图片，或使用平台支持的文件引用。"
            )
        if provider["name"] == "minimax":
            print("[警告] M3 官方文档仅列出 image_url / video_url，file_url 可能不受支持", file=sys.stderr)
        content.append({"type": "file_url", "file_url": {"url": item}})
    if prompt:
        content.append({"type": "text", "text": prompt})
    return content


def build_payload(provider: dict, content: list, thinking: str | None, max_tokens: int | None = None) -> dict:
    payload = {
        "model": provider["model"],
        "messages": [{"role": "user", "content": content}],
    }
    if max_tokens:
        key = "max_completion_tokens" if provider["name"] == "minimax" else "max_tokens"
        payload[key] = max_tokens
    if thinking:
        if provider["name"] == "minimax":
            payload["thinking"] = {"type": "adaptive" if thinking == "enabled" else "disabled"}
        else:
            payload["thinking"] = {"type": thinking}
    return payload


def call_provider(provider: dict, payload: dict, timeout: int) -> dict:
    payload_bytes = json.dumps(payload).encode("utf-8")
    if len(payload_bytes) > REQUEST_BODY_MAX_BYTES:
        raise PayloadTooLargeError(
            f"请求体 {len(payload_bytes) / 1024 / 1024:.1f}MB 超过 64MB 上限"
        )
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error = ""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {e.code}: {body}"
            if (e.code == 429 or e.code >= 500) and attempt < 3:
                wait = 5 * (2 ** attempt)
                retry_after = e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = min(int(retry_after), 60)
                print(f"[{provider['name']} HTTP {e.code}] {wait} 秒后重试（第 {attempt + 1} 次）...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 422 and "sensitive" in body.lower():
                last_error += "（可能触发内容安全过滤，请调整图片/描述后重试，或使用 --provider glm）"
            break
        except urllib.error.URLError as e:
            last_error = f"网络错误: {e.reason}"
            break
        except TimeoutError:
            last_error = "请求超时"
            break
    raise RuntimeError(f"[{provider['name']}] {last_error}")


def call_image_generation(provider: dict, payload: dict, timeout: int) -> dict:
    url = provider["base_url"].rstrip("/") + "/image_generation"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"生图 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"生图网络错误: {e.reason}")
    base = data.get("base_resp") or {}
    if base.get("status_code") != 0:
        raise RuntimeError(f"生图失败: {json.dumps(data, ensure_ascii=False)}")
    return data


def download_image(url: str, out_dir: Path, index: int, timeout: int) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "vision-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=max(timeout, 120)) as resp:
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
        ext = mimetypes.guess_extension(ctype) or ".png"
        if ext == ".jpe":
            ext = ".jpg"
        dest = out_dir / f"image_{index}{ext}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
    return dest


def run_generate(provider: dict, args) -> None:
    payload = {
        "model": args.gen_model,
        "prompt": args.generate,
        "response_format": "url",
    }
    if args.aspect_ratio:
        payload["aspect_ratio"] = args.aspect_ratio
    if args.width is not None:
        payload["width"] = args.width
        payload["height"] = args.height
    if args.style:
        payload["style"] = args.style
    if args.count != 1:
        payload["n"] = args.count
    if args.seed is not None:
        payload["seed"] = args.seed
    if args.prompt_optimizer:
        payload["prompt_optimizer"] = True
    if args.watermark:
        payload["watermark"] = True

    data = call_image_generation(provider, payload, args.timeout)
    urls = (data.get("data") or {}).get("image_urls") or []
    if not urls:
        raise RuntimeError(f"生图响应中没有图片: {json.dumps(data, ensure_ascii=False)}")
    metadata = data.get("metadata") or {}
    print(f"[生成] {provider['name']} ({args.gen_model}) 成功 {len(urls)} 张", file=sys.stderr)
    for i, url in enumerate(urls, 1):
        print(f"[{i}] {url}")
        if args.save_dir:
            out_dir = Path(args.save_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = download_image(url, out_dir, i, args.timeout)
            print(f"    {dest}")


def render_message(msg: dict) -> None:
    if msg.get("reasoning_content"):
        print("[思考过程]")
        print(msg["reasoning_content"])
        print()
    content = msg.get("content")
    if content is None:
        return
    if isinstance(content, str):
        print(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                print(part)
            elif isinstance(part, dict):
                if part.get("type") == "text":
                    print(part.get("text", ""))
                elif part.get("type") == "image_url":
                    print(f"[图片输出: {part.get('image_url', {}).get('url', '')}]")
    else:
        print(str(content))


def print_usage(provider: dict, data: dict) -> None:
    usage = data.get("usage") or {}
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    parts = [f"{k}={usage.get(k)}" for k in keys if usage.get(k) is not None]
    if parts:
        print(f"[提供商] {provider['name']} ({provider['model']})  用量: {', '.join(parts)}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="多模态分析入口：MiniMax-M3 主识图/视频，GLM 自动降级备用",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("images", nargs="*", help="本地图片/视频路径或 http(s)/data URL（可多个，自动识别类型）")
    parser.add_argument("-p", "--prompt", help="提问内容；缺省时从 stdin 读取")
    parser.add_argument("-m", "--model", help="覆盖主提供商（primary）的模型名")
    parser.add_argument("--provider", choices=["minimax", "glm"], help="强制只用指定提供商")
    parser.add_argument("--video", action="append", default=[], help="视频：本地路径 / URL / mm_file://{file_id}（可多次）")
    parser.add_argument("--file", action="append", default=[], help="文件：仅 http(s) URL 或 mm_file://{file_id}（可多次）")
    parser.add_argument("--detail", choices=["low", "default", "high"], help="图像/视频细节级别，默认 default")
    parser.add_argument("--fps", type=float, help="视频抽帧帧率，范围 0.2-5，默认 1（长视频未指定时自动降低）")
    parser.add_argument("--max-long-side-pixel", type=int, help="控制最长边像素（图片/视频）")
    parser.add_argument("--upload", action="store_true", help="本地视频强制走 Files API 上传（即使小于 45MB）")
    parser.add_argument("--generate", help="文生图：传入图片描述（≤1500 字符），生成图片；此模式下忽略图片/视频输入")
    parser.add_argument("--gen-model", choices=["image-01", "image-01-live"], default="image-01", help="生图模型，默认 image-01")
    parser.add_argument("--aspect-ratio", choices=["1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"], help="图像宽高比，默认 1:1")
    parser.add_argument("--width", type=int, help="生成宽度（像素），仅 image-01，需与 --height 同用，512-2048 且为 8 的倍数")
    parser.add_argument("--height", type=int, help="生成高度（像素），仅 image-01，需与 --width 同用")
    parser.add_argument("--style", help="画风设置，仅 image-01-live")
    parser.add_argument("--count", type=int, default=1, help="单次生成数量（对应 API n 参数），1-9，默认 1")
    parser.add_argument("--seed", type=int, help="随机种子，相同 seed 与参数可复现相近结果")
    parser.add_argument("--prompt-optimizer", action="store_true", help="开启 prompt 自动优化")
    parser.add_argument("--watermark", action="store_true", help="在生成的图片中添加水印")
    parser.add_argument("--save-dir", help="下载生成图片到本地目录（默认只输出 URL）")
    parser.add_argument("--thinking", action="store_true", help="开启深度思考模式")
    parser.add_argument("--no-thinking", action="store_true", help="显式关闭深度思考模式")
    parser.add_argument("--api-key", help="临时覆盖主提供商 API Key")
    parser.add_argument("--json", action="store_true", help="输出完整原始 JSON 响应")
    parser.add_argument("--timeout", type=int, default=180, help="单次请求超时秒数，默认 180（大文件上传可调大）")
    parser.add_argument("--max-tokens", type=int, help="限制回答最大 token 数（批量检查推荐 300-900）")
    parser.add_argument("--concise", action="store_true", help="要求简洁中文回答（≤200字、不用 emoji/表格），默认 max-tokens=400")
    args = parser.parse_args()

    if args.generate:
        if args.images or args.video or args.file:
            parser.error("--generate 模式下请勿同时传入图片/视频/文件")
        if len(args.generate) > 1500:
            parser.error("生图 prompt 最长 1500 字符")
        if (args.width is None) != (args.height is None):
            parser.error("--width 和 --height 必须同时设置")
        for name, value in (("--width", args.width), ("--height", args.height)):
            if value is not None and (not 512 <= value <= 2048 or value % 8 != 0):
                parser.error(f"{name} 必须在 512-2048 之间且为 8 的倍数")
        if args.width is not None and args.gen_model != "image-01":
            parser.error("--width/--height 仅支持 image-01 模型")
        if args.style and args.gen_model != "image-01-live":
            parser.error("--style 仅支持 image-01-live 模型")
        if args.aspect_ratio == "21:9" and args.gen_model != "image-01":
            parser.error("21:9 宽高比仅支持 image-01 模型")
        if not 1 <= args.count <= 9:
            parser.error("--count 范围 1-9")
        providers = load_providers(args.provider, args.model, args.api_key)
        gen_provider = next((p for p in providers if p["name"] == "minimax"), None)
        if gen_provider is None:
            sys.exit("文生图仅支持 MiniMax 提供商（image-01 / image-01-live），请检查配置。")
        run_generate(gen_provider, args)
        return

    if not args.images and not args.video and not args.file:
        parser.error("至少需要一张图片或一个视频（或 --file）")
    if args.fps is not None and not (0.2 <= args.fps <= 5):
        parser.error("--fps 必须在 0.2 到 5 之间")
    if args.max_long_side_pixel is not None and args.max_long_side_pixel <= 0:
        parser.error("--max-long-side-pixel 必须为正整数")
    if args.max_tokens is not None and args.max_tokens <= 0:
        parser.error("--max-tokens 必须为正整数")

    prompt = args.prompt
    if prompt is None:
        prompt = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    if not prompt:
        prompt = "请详细描述这张图片或视频的内容，包括所有可见文字、主体、布局和关键细节，用中文回答。"
    max_tokens = args.max_tokens
    if args.concise:
        if max_tokens is None:
            max_tokens = 400
        prompt = prompt.rstrip() + "\n\n请用中文简洁回答：不要使用 emoji 和 Markdown 表格，控制在 200 字以内。"

    # 位置参数自动识别：视频扩展名归入 --video 通道，其余按图片处理
    images = []
    videos = list(args.video)
    for item in args.images:
        if _suffix_of(item) in VIDEO_EXTS:
            videos.append(item)
        else:
            images.append(item)

    thinking = None
    if args.thinking:
        thinking = "enabled"
    elif args.no_thinking:
        thinking = "disabled"

    providers = load_providers(args.provider, args.model, args.api_key)
    upload_cache = {}
    errors = []
    for provider in providers:
        if videos and provider["name"] != "minimax":
            print(f"[警告] {provider['name']} 可能不支持视频输入（历史上 GLM 对视频返回图片格式错误），若失败将终止", file=sys.stderr)
        data = None
        for tight in (False, True):
            try:
                content = build_content(
                    provider,
                    images,
                    videos,
                    args.file,
                    prompt,
                    args.detail,
                    args.fps,
                    args.max_long_side_pixel,
                    args.upload,
                    upload_cache,
                    args.timeout,
                    tight=tight,
                )
                payload = build_payload(provider, content, thinking, max_tokens)
                data = call_provider(provider, payload, args.timeout)
                break
            except PayloadTooLargeError as e:
                if not tight:
                    print(f"⚠ {e}，自动以更小图片体积重试...", file=sys.stderr)
                    continue
                errors.append(f"[{provider['name']}] {e}，压缩后仍超限，请减少图片/视频数量")
                print(f"✗ {errors[-1]}", file=sys.stderr)
                break
            except RuntimeError as e:
                errors.append(str(e))
                print(f"⚠ {e}，切换到下一个提供商..." if len(providers) > 1 else f"✗ {e}", file=sys.stderr)
                break
        if data is None:
            continue
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            errors.append(f"[{provider['name']}] 响应格式异常: {json.dumps(data, ensure_ascii=False)}")
            continue
        print_usage(provider, data)
        render_message(msg)
        return

    sys.exit("所有提供商均失败:\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
