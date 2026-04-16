"""Bilibili favorites auto-download monitor.

Polls configured Bilibili favorites folders for newly added videos,
downloads them via yt-dlp, and sends text notifications to Telegram.
"""

import html
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import debug_bus
from bilibili_cookies import USER_AGENT, _parse_cookie_value, check_cookie_valid

logger = logging.getLogger(__name__)

# Rolling window caps
_MAX_DOWNLOADED_BVIDS = 5000
_MAX_HISTORY = 50


class BilibiliFavMonitorHandler:
    def __init__(
        self,
        cookies_path: str,
        state_path: str,
        download_dir: str,
        download_timeout: int = 600,
        check_interval: int = 300,
        initial_download_limit: int = 0,
        proxy: str = "",
        nas_enabled: bool = False,
        nas_host: str = "nas",
        nas_dest_dir: str = "/volume1/Share/BilibiliVideos",
        telegram_client=None,
        shutdown_event: threading.Event | None = None,
    ):
        self._cookies_path = Path(cookies_path).expanduser() if cookies_path else None
        self._state_path = Path(state_path)
        self._download_dir = Path(download_dir)
        self._download_timeout = download_timeout
        self._check_interval = check_interval
        self._initial_download_limit = initial_download_limit
        self._proxy = proxy
        self._nas_enabled = nas_enabled
        self._nas_host = nas_host
        self._nas_dest_dir = nas_dest_dir
        self._client = telegram_client
        self._shutdown_event = shutdown_event or threading.Event()

        self._state_lock = threading.Lock()
        self._paused = threading.Event()  # set = paused
        self._check_now_event = threading.Event()
        self._queue: queue.Queue[dict] = queue.Queue()
        self._current_download: dict | None = None

        # Cached user mid
        self._user_mid: int | None = None

        # State
        self._state: dict = {
            "monitored_folders": {},
            "downloaded_bvids": [],
            "download_history": [],
            "pending_queue": [],
        }
        self._load_state()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Launch monitor and downloader daemon threads."""
        # Restore pending queue from state
        for item in self._state.get("pending_queue", []):
            self._queue.put(item)

        self._download_dir.mkdir(parents=True, exist_ok=True)

        monitor = threading.Thread(
            target=self._monitor_thread,
            name="bilibili-fav-monitor",
            daemon=True,
        )
        downloader = threading.Thread(
            target=self._downloader_thread,
            name="bilibili-fav-downloader",
            daemon=True,
        )
        monitor.start()
        downloader.start()
        logger.info(
            "Bilibili fav monitor started (interval=%ds, folders=%d, pending=%d)",
            self._check_interval,
            len(self._state["monitored_folders"]),
            self._queue.qsize(),
        )

    # ------------------------------------------------------------------
    # Command interface
    # ------------------------------------------------------------------

    def handle_command(self, subcommand: str) -> str | None:
        sub = subcommand.strip()
        sub_lower = sub.lower()

        if not sub_lower or sub_lower == "status":
            result = self._cmd_status()
        elif sub_lower == "folders":
            result = self._cmd_folders()
        elif sub_lower == "list":
            result = self._cmd_list()
        elif sub_lower.startswith("add "):
            result = self._cmd_add(sub[4:].strip())
        elif sub_lower.startswith("remove "):
            result = self._cmd_remove(sub[7:].strip())
        elif sub_lower == "check":
            result = self._cmd_check()
        elif sub_lower == "pause":
            result = self._cmd_pause()
        elif sub_lower == "resume":
            result = self._cmd_resume()
        elif sub_lower == "queue":
            result = self._cmd_queue()
        elif sub_lower.startswith("download "):
            result = self._cmd_download(sub[9:].strip())
        elif sub_lower == "sync":
            result = self._cmd_sync()
        elif sub_lower.startswith("history"):
            result = self._cmd_history(sub[7:].strip())
        else:
            result = (
                "<b>B站收藏夹监控命令</b>\n"
                "<code>/fav</code> — 查看状态\n"
                "<code>/fav folders</code> — 列出所有收藏夹\n"
                "<code>/fav list</code> — 查看监控中的收藏夹\n"
                "<code>/fav add &lt;ID&gt;</code> — 添加收藏夹监控\n"
                "<code>/fav remove &lt;ID&gt;</code> — 移除收藏夹监控\n"
                "<code>/fav download &lt;ID&gt;</code> — 全量下载收藏夹\n"
                "<code>/fav check</code> — 立即检查\n"
                "<code>/fav sync</code> — 同步本地文件到 NAS\n"
                "<code>/fav pause</code> — 暂停监控\n"
                "<code>/fav resume</code> — 恢复监控\n"
                "<code>/fav queue</code> — 查看下载队列\n"
                "<code>/fav history [N]</code> — 下载历史"
            )

        if result and self._client:
            self._client.send_message(result, parse_mode="HTML")
        return None

    def _cmd_status(self) -> str:
        with self._state_lock:
            folders = len(self._state["monitored_folders"])
            downloaded = len(self._state["downloaded_bvids"])
            history = self._state["download_history"]
            last_ok = history[-1]["downloaded_at"] if history else "N/A"

        status = "暂停" if self._paused.is_set() else "运行中"
        pending = self._queue.qsize()
        cur = self._current_download
        cur_text = f"\n当前下载: {cur['title']}" if cur else ""

        return (
            f"<b>B站收藏夹监控</b>\n"
            f"状态: {status}\n"
            f"监控收藏夹: {folders}\n"
            f"已下载: {downloaded}\n"
            f"队列等待: {pending}{cur_text}\n"
            f"检查间隔: {self._check_interval}s\n"
            f"上次下载: {last_ok}"
        )

    def _cmd_folders(self) -> str:
        mid = self._get_user_mid()
        if not mid:
            return "无法获取用户信息，请检查 B站 cookie 是否有效。"
        folders = self._api_list_folders(mid)
        if not folders:
            return "未找到任何收藏夹。"

        with self._state_lock:
            monitored = set(self._state["monitored_folders"].keys())

        lines = ["<b>B站收藏夹列表</b>\n"]
        for f in folders:
            fid = str(f["id"])
            mark = " [监控中]" if fid in monitored else ""
            lines.append(
                f"  <code>{fid}</code> — {html.escape(f['title'])} "
                f"({f['media_count']}个){mark}"
            )
        return "\n".join(lines)

    def _cmd_list(self) -> str:
        with self._state_lock:
            folders = self._state["monitored_folders"]
        if not folders:
            return "当前没有监控任何收藏夹。使用 <code>/fav folders</code> 查看可用列表。"
        lines = ["<b>监控中的收藏夹</b>\n"]
        for fid, info in folders.items():
            lines.append(f"  <code>{fid}</code> — {html.escape(info['title'])}")
        return "\n".join(lines)

    def _cmd_add(self, arg: str) -> str:
        media_id = arg.strip()
        if not media_id.isdigit():
            return "用法: <code>/fav add &lt;收藏夹ID&gt;</code>\n使用 <code>/fav folders</code> 查看 ID。"

        with self._state_lock:
            if media_id in self._state["monitored_folders"]:
                return f"收藏夹 {media_id} 已在监控中。"

        # Verify folder exists and get title
        mid = self._get_user_mid()
        if not mid:
            return "无法获取用户信息，请检查 B站 cookie。"

        folders = self._api_list_folders(mid)
        folder_info = None
        for f in folders or []:
            if str(f["id"]) == media_id:
                folder_info = f
                break
        if not folder_info:
            return f"未找到 ID 为 {media_id} 的收藏夹。"

        title = folder_info["title"]

        # Seed existing videos as known
        items = self._api_fetch_all_items(int(media_id))
        existing_bvids = [
            it["bvid"] for it in items
            if it.get("bvid") and it.get("title") != "已失效视频" and it.get("type", 0) == 2
        ]

        with self._state_lock:
            self._state["monitored_folders"][media_id] = {
                "title": title,
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
            # Mark all existing as known
            known = set(self._state["downloaded_bvids"])
            for bvid in existing_bvids:
                if bvid not in known:
                    self._state["downloaded_bvids"].append(bvid)
                    known.add(bvid)

            # Optionally queue recent ones for download
            queued = 0
            if self._initial_download_limit > 0:
                to_download = existing_bvids[: self._initial_download_limit]
                for bvid in reversed(to_download):
                    # Remove from known so they get downloaded
                    if bvid in known:
                        self._state["downloaded_bvids"].remove(bvid)
                        known.discard(bvid)
                    item_title = next(
                        (it["title"] for it in items if it.get("bvid") == bvid), bvid
                    )
                    task = {
                        "bvid": bvid,
                        "title": item_title,
                        "fav_id": media_id,
                        "fav_title": title,
                    }
                    self._queue.put(task)
                    self._state["pending_queue"].append(task)
                    queued += 1

            self._trim_bvids()
            self._save_state()

        msg = (
            f"已添加收藏夹监控: <b>{html.escape(title)}</b> (ID: {media_id})\n"
            f"已标记 {len(existing_bvids)} 个现有视频"
        )
        if queued > 0:
            msg += f"\n已加入下载队列: {queued} 个"
        return msg

    def _cmd_remove(self, arg: str) -> str:
        media_id = arg.strip()
        with self._state_lock:
            if media_id not in self._state["monitored_folders"]:
                return f"收藏夹 {media_id} 不在监控列表中。"
            title = self._state["monitored_folders"].pop(media_id)["title"]
            self._save_state()
        return f"已移除收藏夹监控: <b>{html.escape(title)}</b> (ID: {media_id})"

    def _cmd_download(self, arg: str) -> str:
        media_id = arg.strip()
        if not media_id.isdigit():
            return "用法: <code>/fav download &lt;收藏夹ID&gt;</code>"

        # Determine folder title — check monitored folders first, else query API
        with self._state_lock:
            info = self._state["monitored_folders"].get(media_id)
        if info:
            fav_title = info["title"]
        else:
            mid = self._get_user_mid()
            if not mid:
                return "无法获取用户信息，请检查 B站 cookie。"
            folders = self._api_list_folders(mid)
            match = next((f for f in (folders or []) if str(f["id"]) == media_id), None)
            if not match:
                return f"未找到 ID 为 {media_id} 的收藏夹。"
            fav_title = match["title"]

        # Fetch all items
        items = self._api_fetch_all_items(int(media_id))
        valid = [
            it for it in items
            if it.get("bvid") and it.get("title") != "已失效视频" and it.get("type", 0) == 2
        ]
        if not valid:
            return "该收藏夹没有可下载的视频。"

        # Collect bvids already in queue to avoid duplicates
        with self._queue.mutex:
            queued_bvids = {item["bvid"] for item in self._queue.queue}
        cur = self._current_download
        if cur:
            queued_bvids.add(cur["bvid"])

        count = 0
        with self._state_lock:
            known = set(self._state["downloaded_bvids"])
            for it in reversed(valid):  # oldest first into queue
                bvid = it["bvid"]
                if bvid in queued_bvids:
                    continue
                # Remove from known so downloader will process it
                if bvid in known:
                    try:
                        self._state["downloaded_bvids"].remove(bvid)
                    except ValueError:
                        pass
                    known.discard(bvid)
                task = {
                    "bvid": bvid,
                    "title": it["title"],
                    "fav_id": media_id,
                    "fav_title": fav_title,
                }
                self._queue.put(task)
                self._state["pending_queue"].append(task)
                count += 1
            self._save_state()

        return (
            f"收藏夹 <b>{html.escape(fav_title)}</b> 全量下载已启动\n"
            f"有效视频: {len(valid)}，新加入队列: {count}，"
            f"跳过（已在队列/已下载）: {len(valid) - count}"
        )

    def _cmd_check(self) -> str:
        self._check_now_event.set()
        return "已触发立即检查。"

    def _cmd_sync(self) -> str:
        if not self._nas_enabled:
            return "NAS 同步未启用。请在 config.json 中设置 <code>bilibili_fav_nas_enabled: true</code>。"
        self._queue.put({"_action": "sync_all"})
        return "已加入同步任务到队列，将同步所有本地未同步文件到 NAS。"

    def _cmd_pause(self) -> str:
        self._paused.set()
        return "收藏夹监控已暂停。使用 <code>/fav resume</code> 恢复。"

    def _cmd_resume(self) -> str:
        self._paused.clear()
        return "收藏夹监控已恢复。"

    def _cmd_queue(self) -> str:
        cur = self._current_download
        # Snapshot queue contents
        with self._queue.mutex:
            pending = list(self._queue.queue)

        lines = ["<b>下载队列</b>\n"]
        if cur:
            lines.append(f"正在下载:\n  {html.escape(cur['title'])} (<code>{cur['bvid']}</code>)")
        else:
            lines.append("正在下载: 无")

        if pending:
            lines.append(f"\n等待中 ({len(pending)}):")
            for i, item in enumerate(pending[:20], 1):
                lines.append(f"  {i}. {html.escape(item['title'])} (<code>{item['bvid']}</code>)")
            if len(pending) > 20:
                lines.append(f"  ... 还有 {len(pending) - 20} 个")
        else:
            lines.append("\n等待中: 无")

        return "\n".join(lines)

    def _cmd_history(self, arg: str) -> str:
        try:
            n = int(arg) if arg else 10
        except ValueError:
            n = 10
        n = max(1, min(n, _MAX_HISTORY))

        with self._state_lock:
            history = self._state["download_history"][-n:]

        if not history:
            return "暂无下载记录。"

        lines = [f"<b>最近 {len(history)} 条下载记录</b>\n"]
        for entry in reversed(history):
            status_icon = "OK" if entry["status"] == "success" else "FAIL"
            lines.append(
                f"  [{status_icon}] {html.escape(entry['title'][:40])}\n"
                f"        {entry['bvid']} | {entry['downloaded_at'][:16]}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Monitor thread
    # ------------------------------------------------------------------

    def _monitor_thread(self):
        logger.info("Fav monitor thread started")
        retries = 0
        while not self._shutdown_event.is_set():
            # Pause check
            while self._paused.is_set() and not self._shutdown_event.is_set():
                self._shutdown_event.wait(timeout=5)
            if self._shutdown_event.is_set():
                break

            try:
                new_count = self._check_favorites()
                retries = 0
                debug_bus.emit("bilibili_fav_check", {
                    "folders": len(self._state["monitored_folders"]),
                    "new_videos": new_count,
                })
            except Exception as e:
                retries += 1
                backoff = min(30 * (2 ** retries), 600)
                logger.warning("Fav check error (retry %d, backoff %ds): %s", retries, backoff, e)
                debug_bus.emit("bilibili_fav_error", {"error": str(e)})
                self._shutdown_event.wait(timeout=backoff)
                continue

            # Interruptible sleep
            if self._check_now_event.is_set():
                self._check_now_event.clear()
            else:
                self._check_now_event.wait(timeout=self._check_interval)
                self._check_now_event.clear()

        logger.info("Fav monitor thread stopped")

    def _check_favorites(self) -> int:
        """Check all monitored folders for new videos. Returns count of new items queued."""
        if not self._cookies_path:
            return 0

        # Cookie validation
        if not check_cookie_valid(self._cookies_path):
            logger.warning("Bilibili cookie invalid, skipping fav check")
            return 0

        with self._state_lock:
            folders = dict(self._state["monitored_folders"])

        if not folders:
            return 0

        total_new = 0
        for fav_id, info in folders.items():
            if self._shutdown_event.is_set():
                break
            new = self._check_single_folder(fav_id, info["title"])
            total_new += new

        return total_new

    def _check_single_folder(self, fav_id: str, fav_title: str) -> int:
        """Check a single folder for new items. Returns count of new items queued."""
        items = self._api_fetch_items(int(fav_id), pn=1, ps=20)
        if not items:
            return 0

        with self._state_lock:
            known = set(self._state["downloaded_bvids"])

        new_items = []
        for item in items:
            bvid = item.get("bvid", "")
            title = item.get("title", "")
            item_type = item.get("type", 0)

            if not bvid or item_type != 2 or title == "已失效视频":
                continue
            if bvid in known:
                # Already known — since results sorted by mtime, stop here
                break
            new_items.append(item)

        if not new_items:
            return 0

        # Enqueue in chronological order (oldest first)
        count = 0
        with self._state_lock:
            for item in reversed(new_items):
                bvid = item["bvid"]
                if bvid in set(self._state["downloaded_bvids"]):
                    continue
                task = {
                    "bvid": bvid,
                    "title": item["title"],
                    "fav_id": fav_id,
                    "fav_title": fav_title,
                }
                self._queue.put(task)
                self._state["pending_queue"].append(task)
                count += 1
            if count:
                self._save_state()

        logger.info("Found %d new videos in folder %s (%s)", count, fav_id, fav_title)
        return count

    # ------------------------------------------------------------------
    # Downloader thread
    # ------------------------------------------------------------------

    def _downloader_thread(self):
        logger.info("Fav downloader thread started")

        # Sync any previously downloaded but unsynced files on startup
        if self._nas_enabled:
            try:
                self._sync_all_pending()
            except Exception as e:
                logger.warning("Startup NAS sync error: %s", e)

        while not self._shutdown_event.is_set():
            try:
                task = self._queue.get(timeout=5)
            except queue.Empty:
                continue

            # Handle sentinel tasks
            if task.get("_action") == "sync_all":
                try:
                    self._sync_all_pending()
                except Exception as e:
                    logger.warning("NAS sync error: %s", e)
                self._queue.task_done()
                continue

            # Remove from persistent queue
            with self._state_lock:
                pq = self._state["pending_queue"]
                for i, item in enumerate(pq):
                    if item["bvid"] == task["bvid"]:
                        pq.pop(i)
                        break
                self._save_state()

            self._current_download = task
            try:
                self._download_video(task)
            except Exception as e:
                logger.exception("Download failed for %s: %s", task["bvid"], e)
                self._record_history(task, "failed", str(e))
                self._notify_failure(task, str(e))
            finally:
                self._current_download = None
                self._queue.task_done()

        logger.info("Fav downloader thread stopped")

    def _download_video(self, task: dict):
        bvid = task["bvid"]
        title = task["title"]
        fav_title = task["fav_title"]

        url = f"https://www.bilibili.com/video/{bvid}"

        # Create per-folder subdirectory
        safe_folder = re.sub(r'[\\/:*?"<>|]', '_', fav_title).strip() or "default"
        folder_dir = self._download_dir / safe_folder
        folder_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "yt-dlp",
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", "%(title).80s [%(id)s].%(ext)s",
            "--no-playlist",
            "--no-part",
            "--force-overwrites",
            "--print", "after_move:filepath",
        ]
        if self._cookies_path and self._cookies_path.exists():
            cmd.extend(["--cookies", str(self._cookies_path)])
        if self._proxy:
            cmd.extend(["--proxy", self._proxy])
        cmd.append(url)

        logger.info("Downloading %s: %s", bvid, title)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._download_timeout,
            cwd=str(folder_dir),
        )

        if result.returncode != 0:
            error_msg = (result.stderr or result.stdout or "unknown error")[-500:]
            raise RuntimeError(f"yt-dlp exit {result.returncode}: {error_msg}")

        # Extract filepath from yt-dlp output
        filepath = None
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("["):
                filepath = line
        if not filepath:
            filepath = self._find_latest_file(folder_dir)

        # Mark as downloaded
        with self._state_lock:
            if bvid not in set(self._state["downloaded_bvids"]):
                self._state["downloaded_bvids"].append(bvid)
                self._trim_bvids()
            self._save_state()

        # NAS sync
        nas_status = ""
        if self._nas_enabled and filepath:
            nas_ok = self._sync_to_nas(filepath, safe_folder)
            nas_status = "\nNAS: 已同步" if nas_ok else "\nNAS: 同步失败"

        self._record_history(task, "success")
        self._notify_success(task, filepath, nas_status)
        logger.info("Downloaded %s: %s -> %s", bvid, title, filepath)
        debug_bus.emit("bilibili_fav_download", {
            "bvid": bvid, "title": title, "status": "success",
        })

    def _find_latest_file(self, directory: Path) -> str | None:
        exts = {".mp4", ".mkv", ".webm", ".flv", ".avi"}
        files = [f for f in directory.iterdir() if f.suffix.lower() in exts]
        if not files:
            return None
        return str(max(files, key=lambda f: f.stat().st_mtime))

    # ------------------------------------------------------------------
    # NAS sync
    # ------------------------------------------------------------------

    _VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".flv", ".avi"}

    def _sync_to_nas(self, filepath: str, folder_name: str) -> bool:
        """Rsync a single file to NAS. Returns True on success."""
        filepath = Path(filepath)
        if not filepath.exists():
            logger.warning("NAS sync: file not found: %s", filepath)
            return False

        remote_dir = f"{self._nas_dest_dir}/{folder_name}"

        # Create remote directory
        mkdir_result = subprocess.run(
            ["ssh", self._nas_host, f"mkdir -p {remote_dir}"],
            capture_output=True, text=True, timeout=30,
        )
        if mkdir_result.returncode != 0:
            logger.error("NAS ssh mkdir failed: %s", mkdir_result.stderr)
            return False

        # Rsync with --remove-source-files
        rsync_result = subprocess.run(
            [
                "rsync", "-av", "--remove-source-files",
                "--rsync-path=/usr/bin/rsync",
                str(filepath),
                f"{self._nas_host}:{remote_dir}/{filepath.name}",
            ],
            capture_output=True, text=True, timeout=600,
        )
        if rsync_result.returncode != 0:
            logger.error("NAS rsync failed (rc=%d): %s",
                         rsync_result.returncode, rsync_result.stderr[-500:])
            return False

        logger.info("NAS synced: %s -> %s:%s/%s",
                     filepath.name, self._nas_host, remote_dir, filepath.name)
        return True

    def _sync_all_pending(self):
        """Scan download directory for unsynced video files and sync them to NAS."""
        if not self._download_dir.exists():
            return

        synced = 0
        failed = 0
        for subdir in sorted(self._download_dir.iterdir()):
            if self._shutdown_event.is_set():
                break
            if not subdir.is_dir():
                continue
            folder_name = subdir.name
            files = [
                f for f in subdir.iterdir()
                if f.is_file() and f.suffix.lower() in self._VIDEO_EXTS
            ]
            for f in sorted(files):
                if self._shutdown_event.is_set():
                    break
                if self._sync_to_nas(str(f), folder_name):
                    synced += 1
                else:
                    failed += 1

        if synced or failed:
            msg = f"<b>[NAS 同步]</b> 完成: 成功 {synced} 个"
            if failed:
                msg += f"，失败 {failed} 个"
            logger.info("NAS sync all: synced=%d, failed=%d", synced, failed)
            if self._client:
                self._client.send_message(msg, parse_mode="HTML")

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _notify_success(self, task: dict, filepath: str | None, nas_status: str = ""):
        path_line = f"\n路径: <code>{html.escape(str(filepath))}</code>" if filepath else ""
        msg = (
            f"<b>[收藏夹自动下载]</b>\n\n"
            f"标题: {html.escape(task['title'])}\n"
            f"BV号: <code>{task['bvid']}</code>\n"
            f"收藏夹: {html.escape(task['fav_title'])}\n"
            f"状态: 下载完成"
            f"{path_line}{nas_status}"
        )
        if self._client:
            self._client.send_message(msg, parse_mode="HTML")

    def _notify_failure(self, task: dict, error: str):
        msg = (
            f"<b>[收藏夹自动下载]</b>\n\n"
            f"标题: {html.escape(task['title'])}\n"
            f"BV号: <code>{task['bvid']}</code>\n"
            f"收藏夹: {html.escape(task['fav_title'])}\n"
            f"状态: 下载失败\n"
            f"原因: {html.escape(error[:200])}"
        )
        if self._client:
            self._client.send_message(msg, parse_mode="HTML")
        debug_bus.emit("bilibili_fav_download", {
            "bvid": task["bvid"], "title": task["title"], "status": "failed",
        })

    def _record_history(self, task: dict, status: str, error: str = ""):
        entry = {
            "bvid": task["bvid"],
            "title": task["title"],
            "fav_id": task["fav_id"],
            "fav_title": task["fav_title"],
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
        }
        if error:
            entry["error"] = error[:200]

        with self._state_lock:
            self._state["download_history"].append(entry)
            # Trim history
            if len(self._state["download_history"]) > _MAX_HISTORY:
                self._state["download_history"] = self._state["download_history"][-_MAX_HISTORY:]

            # On failure, still mark as known to avoid retry loops
            if status == "failed":
                bvid = task["bvid"]
                if bvid not in set(self._state["downloaded_bvids"]):
                    self._state["downloaded_bvids"].append(bvid)
                    self._trim_bvids()

            self._save_state()

    # ------------------------------------------------------------------
    # Bilibili API
    # ------------------------------------------------------------------

    def _get_user_mid(self) -> int | None:
        if self._user_mid is not None:
            return self._user_mid

        if not self._cookies_path:
            return None
        sessdata = _parse_cookie_value(self._cookies_path, "SESSDATA")
        if not sessdata:
            return None

        try:
            req = urllib.request.Request(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={
                    "User-Agent": USER_AGENT,
                    "Cookie": f"SESSDATA={sessdata}",
                },
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            if data.get("code") == 0:
                self._user_mid = data["data"]["mid"]
                return self._user_mid
        except Exception as e:
            logger.warning("Failed to get user mid: %s", e)
        return None

    def _build_cookie_header(self) -> str:
        if not self._cookies_path:
            return ""
        sessdata = _parse_cookie_value(self._cookies_path, "SESSDATA")
        return f"SESSDATA={sessdata}" if sessdata else ""

    def _api_list_folders(self, mid: int) -> list[dict] | None:
        cookie = self._build_cookie_header()
        try:
            req = urllib.request.Request(
                f"https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid={mid}",
                headers={"User-Agent": USER_AGENT, "Cookie": cookie},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            if data.get("code") == 0 and data.get("data"):
                return data["data"].get("list", [])
        except Exception as e:
            logger.warning("Failed to list fav folders: %s", e)
        return None

    def _api_fetch_items(self, media_id: int, pn: int = 1, ps: int = 20) -> list[dict]:
        cookie = self._build_cookie_header()
        try:
            url = (
                f"https://api.bilibili.com/x/v3/fav/resource/list"
                f"?media_id={media_id}&pn={pn}&ps={ps}&order=mtime"
            )
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Cookie": cookie},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            if data.get("code") == 0 and data.get("data"):
                return data["data"].get("medias") or []
        except Exception as e:
            logger.warning("Failed to fetch fav items (media_id=%d): %s", media_id, e)
        return []

    def _api_fetch_all_items(self, media_id: int) -> list[dict]:
        """Fetch all items from a folder (paginated). Used for initial seeding."""
        all_items = []
        pn = 1
        while True:
            items = self._api_fetch_items(media_id, pn=pn, ps=40)
            if not items:
                break
            all_items.extend(items)
            if len(items) < 40:
                break
            pn += 1
            # Rate limit courtesy
            time.sleep(0.5)
        return all_items

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        if self._state_path.exists():
            try:
                with open(self._state_path) as f:
                    loaded = json.load(f)
                self._state.update(loaded)
                # Ensure all keys exist
                self._state.setdefault("monitored_folders", {})
                self._state.setdefault("downloaded_bvids", [])
                self._state.setdefault("download_history", [])
                self._state.setdefault("pending_queue", [])
            except Exception as e:
                logger.warning("Failed to load fav state: %s", e)

    def _save_state(self):
        """Atomic write state to disk. Caller must hold _state_lock."""
        tmp = self._state_path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            tmp.replace(self._state_path)
        except Exception as e:
            logger.warning("Failed to save fav state: %s", e)
            if tmp.exists():
                tmp.unlink()

    def _trim_bvids(self):
        """Trim downloaded_bvids to rolling window cap. Caller must hold _state_lock."""
        bvids = self._state["downloaded_bvids"]
        if len(bvids) > _MAX_DOWNLOADED_BVIDS:
            self._state["downloaded_bvids"] = bvids[-_MAX_DOWNLOADED_BVIDS:]
