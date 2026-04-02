"""VideoDownloadHandler — B站 / 抖音视频下载 Skill.

触发命令：/dl <URL>
支持：
  - Bilibili（B站）：4K/HDR 优先，需登录 cookie 才能解锁大会员画质
  - 抖音（Douyin）：最高画质
下载完成后：
  - 文件 < 50MB：直接用 sendVideo 上传到 Telegram
  - 文件 >= 50MB：告知本地路径（不传输大文件）
"""

import logging
import os
import re
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Telegram Bot API 上传限制（字节）
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # 50 MB

# 支持的域名特征
BILIBILI_PATTERN = re.compile(r"(bilibili\.com|b23\.tv)", re.IGNORECASE)
DOUYIN_PATTERN = re.compile(r"(douyin\.com|v\.douyin\.com|iesdouyin\.com)", re.IGNORECASE)


class VideoDownloadHandler:
    def __init__(
        self,
        download_dir: str = "~/video_downloads",
        cookies_bilibili: str = "",
        proxy: str = "",
        timeout: int = 600,
        telegram_client=None,
    ):
        self.download_dir = Path(download_dir).expanduser()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.cookies_bilibili = cookies_bilibili
        self.proxy = proxy
        self.timeout = timeout
        self.client = telegram_client

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
            reply_fn("⏳ 正在获取视频信息，请稍候…")
            cmd = self._build_command(url)
            logger.info("yt-dlp command: %s", " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=str(self.download_dir),
            )

            if result.returncode != 0:
                err = (result.stderr or result.stdout or "未知错误")[-1500:]
                reply_fn(f"❌ 下载失败：\n<pre><code class=\"language-text\">{self._escape(err)}</code></pre>")
                return

            # 从输出里找到下载的文件路径
            filepath = self._find_downloaded_file(result.stdout + result.stderr)
            if not filepath:
                # 兜底：找最新文件
                filepath = self._find_latest_file()

            if not filepath or not filepath.exists():
                reply_fn("❌ 下载完成但找不到输出文件，请检查下载目录。")
                return

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

        except subprocess.TimeoutExpired:
            reply_fn(f"❌ 下载超时（超过 {self.timeout // 60} 分钟）。")
        except Exception as e:
            logger.exception("VideoDownloadHandler error: %s", e)
            reply_fn(f"❌ 下载出错：{self._escape(str(e))}")

    def _build_command(self, url: str) -> list[str]:
        """根据 URL 类型构建 yt-dlp 命令。"""
        cmd = ["yt-dlp"]

        if BILIBILI_PATTERN.search(url):
            # B站：优先 4K HDR > 4K > 1080P60 > 最佳
            # 视频/音频分流，ffmpeg 合并为 mp4
            cmd += [
                "-f", (
                    "bestvideo[height>=2160][ext=mp4]+bestaudio[ext=m4a]"
                    "/bestvideo[height>=2160]+bestaudio"
                    "/bestvideo[height>=1080][fps>=60][ext=mp4]+bestaudio[ext=m4a]"
                    "/bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]"
                    "/bestvideo+bestaudio"
                    "/best"
                ),
                "--merge-output-format", "mp4",
            ]
            if self.cookies_bilibili and Path(self.cookies_bilibili).exists():
                cmd += ["--cookies", self.cookies_bilibili]
                logger.info("Using bilibili cookies: %s", self.cookies_bilibili)
        else:
            # 抖音 / 通用：最高画质
            cmd += [
                "-f", "bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
            ]

        # 代理
        if self.proxy:
            cmd += ["--proxy", self.proxy]

        # 输出模板
        cmd += [
            "-o", "%(title).80s [%(id)s].%(ext)s",
            "--no-playlist",           # 不下整个播放列表，只下单个视频
            "--no-part",               # 不留 .part 临时文件
            "--print", "after_move:filepath",  # 打印最终路径（yt-dlp >= 2021.11）
            url,
        ]
        return cmd

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
