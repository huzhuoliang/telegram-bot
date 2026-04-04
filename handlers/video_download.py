"""VideoDownloadHandler — B站 / 抖音视频下载 Skill.

触发命令：/dl <URL>
支持：
  - Bilibili（B站）：yt-dlp，4K/HDR 优先，需登录 cookie 才能解锁大会员画质
  - 抖音（Douyin）：TikTokDownloader API（Docker 容器），无水印最高画质
  - 其他：yt-dlp 通用下载
下载完成后：
  - 文件 < 50MB：直接用 sendVideo 上传到 Telegram
  - 文件 >= 50MB：告知本地路径（不传输大文件）
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Telegram Bot API 上传限制（字节）
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50 MB

# 支持的域名特征
BILIBILI_PATTERN = re.compile(r"(bilibili\.com|b23\.tv)", re.IGNORECASE)
DOUYIN_PATTERN = re.compile(r"(douyin\.com|v\.douyin\.com|iesdouyin\.com)", re.IGNORECASE)

# 从各种抖音链接中提取视频 ID
DOUYIN_ID_PATTERNS = [
    re.compile(r"douyin\.com/(?:video|note|slides)/(\d{19})"),
    re.compile(r"modal_id=(\d{19})"),
    re.compile(r"\b(\d{19})\b"),
]


class VideoDownloadHandler:
    DOUYIN_COOKIE_MAX_AGE = 3600  # 1 hour
    DOUYIN_API_URL = "http://127.0.0.1:5555/douyin/detail"

    def __init__(
        self,
        download_dir: str = "~/video_downloads",
        cookies_bilibili: str = "",
        cookies_douyin: str = "",
        proxy: str = "",
        timeout: int = 600,
        telegram_client=None,
    ):
        self.download_dir = Path(download_dir).expanduser()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.cookies_bilibili = str(Path(cookies_bilibili).expanduser()) if cookies_bilibili else ""
        self.cookies_douyin = Path(cookies_douyin).expanduser() if cookies_douyin else Path("~/douyin_cookies.txt").expanduser()
        self.proxy = proxy
        self.timeout = timeout
        self.client = telegram_client
        self._douyin_cookie_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def handle(self, url: str, reply_fn) -> None:
        """异步下载：立刻返回，在后台线程执行，完成后通过 reply_fn / client 回复。"""
        t = threading.Thread(
            target=self._download_and_reply,
            args=(url, reply_fn),
            daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------

    def _download_and_reply(self, url: str, reply_fn) -> None:
        try:
            if DOUYIN_PATTERN.search(url):
                self._download_douyin(url, reply_fn)
            else:
                self._download_ytdlp(url, reply_fn)
        except Exception as e:
            logger.exception("VideoDownloadHandler error: %s", e)
            reply_fn(f"❌ 下载出错：{self._escape(str(e))}")

    # ------------------------------------------------------------------
    # 抖音：TikTokDownloader API
    # ------------------------------------------------------------------

    def _get_douyin_cookie_str(self) -> str:
        """Get cookie string, refresh if stale."""
        cookie_str_path = self.cookies_douyin.with_suffix(".str")
        with self._douyin_cookie_lock:
            if cookie_str_path.exists():
                age = time.time() - cookie_str_path.stat().st_mtime
                if age < self.DOUYIN_COOKIE_MAX_AGE:
                    return cookie_str_path.read_text().strip()
            try:
                from douyin_cookies import refresh_cookies
                cookie_str = refresh_cookies(self.cookies_douyin)
                logger.info("Douyin cookies refreshed: %s", self.cookies_douyin)
                return cookie_str
            except Exception as e:
                logger.warning("Failed to refresh douyin cookies: %s", e)
                if cookie_str_path.exists():
                    return cookie_str_path.read_text().strip()
                return ""

    def _resolve_douyin_id(self, url: str) -> str | None:
        """Extract video ID from a Douyin URL. Resolves short links via redirect."""
        for pat in DOUYIN_ID_PATTERNS:
            m = pat.search(url)
            if m:
                return m.group(1)

        # Short link (v.douyin.com) — follow redirect to get full URL
        if "v.douyin.com" in url or "iesdouyin.com" in url:
            try:
                req = urllib.request.Request(url, method="HEAD")
                req.add_header("User-Agent", "Mozilla/5.0")
                resp = urllib.request.urlopen(req, timeout=10)
                final_url = resp.url
                for pat in DOUYIN_ID_PATTERNS:
                    m = pat.search(final_url)
                    if m:
                        return m.group(1)
            except Exception as e:
                logger.warning("Failed to resolve short link %s: %s", url, e)
        return None

    def _download_douyin(self, url: str, reply_fn) -> None:
        reply_fn("⏳ 正在解析抖音视频…")

        # Step 1: Extract video ID
        video_id = self._resolve_douyin_id(url)
        if not video_id:
            reply_fn("❌ 无法从链接中提取抖音视频 ID，请检查链接格式。")
            return

        # Step 2: Get cookie
        cookie = self._get_douyin_cookie_str()

        # Step 3: Call TikTokDownloader API
        payload = json.dumps({
            "detail_id": video_id,
            "cookie": cookie,
            "source": True,
        }).encode()
        req = urllib.request.Request(
            self.DOUYIN_API_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError) as e:
            reply_fn(f"❌ 无法连接抖音解析服务（TikTokDownloader API）：{self._escape(str(e))}")
            return

        data = result.get("data")
        if not data:
            msg = result.get("message", "未知错误")
            reply_fn(f"❌ 解析失败：{self._escape(msg)}")
            return

        # Step 4: Extract best video URL
        title = data.get("desc", "douyin_video")[:80]
        video = data.get("video", {})
        video_url = self._pick_best_douyin_url(video)
        if not video_url:
            reply_fn("❌ 获取到视频信息但无法提取下载地址。")
            return

        # Step 5: Download video file
        reply_fn(f"⏳ 正在下载：{self._escape(title)}")
        safe_title = re.sub(r'[\\/:*?"<>|]', '_', title).strip()
        filepath = self.download_dir / f"{safe_title} [{video_id}].mp4"
        try:
            self._download_file(video_url, filepath)
        except Exception as e:
            reply_fn(f"❌ 视频文件下载失败：{self._escape(str(e))}")
            return

        filepath = self._transcode_av1(filepath, reply_fn)
        self._deliver_video(filepath, reply_fn)

    def _pick_best_douyin_url(self, video: dict) -> str | None:
        """Pick the highest quality video URL from the API response."""
        # Try bit_rate list (sorted by bitrate descending)
        bit_rate = video.get("bit_rate", [])
        if bit_rate:
            bit_rate.sort(key=lambda x: x.get("bit_rate", 0), reverse=True)
            for br in bit_rate:
                urls = br.get("play_addr", {}).get("url_list", [])
                if urls:
                    return urls[0]
        # Fallback to play_addr
        urls = video.get("play_addr", {}).get("url_list", [])
        if urls:
            return urls[0]
        return None

    def _download_file(self, url: str, filepath: Path) -> None:
        """Download a file from URL with proper headers."""
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        req.add_header("Referer", "https://www.douyin.com/")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            with open(filepath, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)

    # ------------------------------------------------------------------
    # yt-dlp (B站 / 通用)
    # ------------------------------------------------------------------

    def _download_ytdlp(self, url: str, reply_fn) -> None:
        reply_fn("⏳ 正在获取视频信息，请稍候…")
        cmd = self._build_ytdlp_command(url)
        logger.info("yt-dlp command: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(self.download_dir),
            )
        except subprocess.TimeoutExpired:
            reply_fn(f"❌ 下载超时（超过 {self.timeout // 60} 分钟）。")
            return

        if result.returncode != 0:
            err = (result.stderr or result.stdout or "未知错误")[-1500:]
            reply_fn(f"❌ 下载失败：\n<pre><code class=\"language-text\">{self._escape(err)}</code></pre>")
            return

        filepath = self._find_downloaded_file(result.stdout + result.stderr)
        if not filepath:
            filepath = self._find_latest_file()

        if not filepath or not filepath.exists():
            reply_fn("❌ 下载完成但找不到输出文件，请检查下载目录。")
            return

        filepath = self._transcode_av1(filepath, reply_fn)
        self._deliver_video(filepath, reply_fn)

    def _build_ytdlp_command(self, url: str) -> list[str]:
        """根据 URL 类型构建 yt-dlp 命令。"""
        cmd = ["yt-dlp"]

        if BILIBILI_PATTERN.search(url):
            cmd += [
                "-f", "bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
            ]
            if self.cookies_bilibili and Path(self.cookies_bilibili).exists():
                cmd += ["--cookies", self.cookies_bilibili]
                logger.info("Using bilibili cookies: %s", self.cookies_bilibili)
        else:
            cmd += [
                "-f", "bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
            ]

        if self.proxy:
            cmd += ["--proxy", self.proxy]

        cmd += [
            "-o", "%(title).80s [%(id)s].%(ext)s",
            "--no-playlist",
            "--no-part",
            "--force-overwrites",
            "--print", "after_move:filepath",
            url,
        ]
        return cmd

    # ------------------------------------------------------------------
    # AV1 → H.265 转码（iPhone Photos 兼容）
    # ------------------------------------------------------------------

    @staticmethod
    def _get_video_codec(filepath: Path) -> str | None:
        """Use ffprobe to get the video codec name."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                 str(filepath)],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def _transcode_av1(self, filepath: Path, reply_fn) -> Path:
        """If video is AV1, transcode to H.265. Returns the (possibly new) path."""
        codec = self._get_video_codec(filepath)
        if codec != "av1":
            return filepath

        reply_fn("⏳ 检测到 AV1 编码，正在转码为 H.265（iPhone 兼容）…")
        out = filepath.with_suffix(".h265.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", str(filepath),
            "-c:v", "libx265", "-preset", "medium", "-crf", "23",
            "-tag:v", "hvc1",  # Apple compatibility tag
            "-c:a", "copy",
            str(out),
        ]
        logger.info("Transcoding AV1 → H.265: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
            if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
                filepath.unlink()
                final = filepath  # reuse original name
                out.rename(final)
                logger.info("Transcode done: %s", final)
                return final
            else:
                logger.warning("Transcode failed (rc=%d): %s", result.returncode, result.stderr[-500:])
                reply_fn("⚠️ 转码失败，将发送 AV1 原始文件")
                if out.exists():
                    out.unlink()
                return filepath
        except subprocess.TimeoutExpired:
            logger.warning("Transcode timed out")
            reply_fn("⚠️ 转码超时，将发送 AV1 原始文件")
            if out.exists():
                out.unlink()
            return filepath

    # ------------------------------------------------------------------
    # 通用：文件交付 + 辅助
    # ------------------------------------------------------------------

    def _deliver_video(self, filepath: Path, reply_fn) -> None:
        """检查大小，上传或告知路径。"""
        file_size = filepath.stat().st_size
        size_mb = file_size / 1024 / 1024

        if file_size <= TELEGRAM_UPLOAD_LIMIT:
            reply_fn(f"✅ 下载完成（{size_mb:.1f} MB），正在上传…")
            self._send_video(filepath, reply_fn)
        else:
            reply_fn(
                f"✅ 下载完成！\n"
                f"📦 大小：<b>{size_mb:.1f} MB</b>（超过 50MB，无法直接发送）\n"
                f"📂 本地路径：<code>{self._escape(str(filepath))}</code>"
            )

    def _find_downloaded_file(self, output: str) -> Path | None:
        """从 yt-dlp stdout 的 --print after_move:filepath 输出找文件路径。"""
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line and os.sep in line or (line.endswith(".mp4") or line.endswith(".mkv") or line.endswith(".webm")):
                p = Path(line)
                if not p.is_absolute():
                    p = self.download_dir / p
                if p.exists():
                    return p
        return None

    def _find_latest_file(self) -> Path | None:
        """兜底：在下载目录中找最新修改的视频文件。"""
        video_exts = {".mp4", ".mkv", ".webm", ".flv", ".avi"}
        files = [
            f for f in self.download_dir.iterdir()
            if f.is_file() and f.suffix.lower() in video_exts
        ]
        if not files:
            return None
        return max(files, key=lambda f: f.stat().st_mtime)

    def _send_video(self, filepath: Path, reply_fn) -> None:
        """通过 TelegramClient 发送视频文件。"""
        if not self.client:
            reply_fn(f"⚠️ 无法上传（未配置 TelegramClient）\n📂 路径：<code>{self._escape(str(filepath))}</code>")
            return
        try:
            self.client.send_video(str(filepath), caption=f"🎬 {self._escape(filepath.stem)}")
        except Exception as e:
            logger.exception("send_video failed: %s", e)
            reply_fn(
                f"⚠️ 上传失败：{self._escape(str(e))}\n"
                f"📂 本地路径：<code>{self._escape(str(filepath))}</code>"
            )

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
