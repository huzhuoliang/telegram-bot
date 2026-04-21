"""Bilibili UP主 video upload monitor.

Monitors specified Bilibili uploaders (UP主) for new video uploads,
sends Telegram notifications, and optionally auto-downloads via yt-dlp.
"""

import hashlib
import html
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import debug_bus
from bilibili_archive import verify_nas_file
from bilibili_cookies import USER_AGENT, _parse_cookie_value, check_cookie_valid

logger = logging.getLogger(__name__)

# Rolling window caps
_MAX_DOWNLOADED_BVIDS = 5000
_MAX_HISTORY = 50

# WBI signing: fixed permutation table
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


class BilibiliUpMonitorHandler:
    def __init__(
        self,
        cookies_path: str,
        state_path: str,
        download_dir: str,
        download_timeout: int = 600,
        check_interval: int = 300,
        proxy: str = "",
        nas_enabled: bool = False,
        nas_host: str = "nas",
        nas_dest_dir: str = "/volume1/Share/BilibiliVideos",
        telegram_client=None,
        shutdown_event: threading.Event | None = None,
        archive=None,
    ):
        self._archive = archive
        self._cookies_path = Path(cookies_path).expanduser() if cookies_path else None
        self._state_path = Path(state_path)
        self._download_dir = Path(download_dir)
        self._download_timeout = download_timeout
        self._check_interval = check_interval
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
        self._redo_queue: queue.Queue[dict] = queue.Queue()  # fast-track queue for /up redo
        self._current_download: dict | None = None
        self._current_activity: str = "idle"  # idle | startup_sync | downloading | nas_syncing

        # WBI key cache
        self._wbi_mixin_key: str | None = None
        self._wbi_key_ts: float = 0.0
        _WBI_KEY_TTL = 43200  # 12 hours

        # State
        self._state: dict = {
            "monitored_ups": {},
            "downloaded_bvids": [],
            "download_history": [],
            "pending_queue": [],
            "paused": False,
        }
        self._load_state()
        # Restore paused flag
        if self._state.get("paused", False):
            self._paused.set()

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
            name="bilibili-up-monitor",
            daemon=True,
        )
        downloader = threading.Thread(
            target=self._downloader_thread,
            name="bilibili-up-downloader",
            daemon=True,
        )
        monitor.start()
        downloader.start()
        logger.info(
            "Bilibili UP monitor started (interval=%ds, ups=%d, pending=%d)",
            self._check_interval,
            len(self._state["monitored_ups"]),
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
        elif sub_lower == "list":
            result = self._cmd_list()
        elif sub_lower.startswith("add "):
            result = self._cmd_add(sub[4:].strip())
        elif sub_lower.startswith("remove "):
            result = self._cmd_remove(sub[7:].strip())
        elif sub_lower.startswith("mode "):
            result = self._cmd_mode(sub[5:].strip())
        elif sub_lower.startswith("download "):
            result = self._cmd_download(sub[9:].strip())
        elif sub_lower.startswith("redo "):
            result = self._cmd_redo(sub[5:].strip())
        elif sub_lower == "rebuild_archive":
            result = self._cmd_rebuild_archive()
        elif sub_lower == "clear_queue":
            result = self._cmd_clear_queue()
        elif sub_lower == "check":
            result = self._cmd_check()
        elif sub_lower == "pause":
            result = self._cmd_pause()
        elif sub_lower == "resume":
            result = self._cmd_resume()
        elif sub_lower == "queue":
            result = self._cmd_queue()
        elif sub_lower == "sync":
            result = self._cmd_sync()
        elif sub_lower.startswith("history"):
            result = self._cmd_history(sub[7:].strip())
        else:
            result = (
                "<b>B站UP主监控命令</b>\n"
                "<code>/up</code> — 查看状态\n"
                "<code>/up list</code> — 查看监控中的UP主\n"
                "<code>/up add &lt;UID&gt;</code> — 添加UP主（仅通知）\n"
                "<code>/up add &lt;UID&gt; --download</code> — 添加UP主（自动下载）\n"
                "<code>/up remove &lt;UID&gt;</code> — 移除UP主\n"
                "<code>/up mode &lt;UID&gt; notify/download</code> — 切换模式\n"
                "<code>/up download &lt;UID&gt;</code> — 下载UP主缺失的视频\n"
                "<code>/up download &lt;UID&gt; --force</code> — 强制重新下载全部\n"
                "<code>/up redo &lt;BV号&gt;</code> — 强制重新下载单个视频（快速通道）\n"
                "<code>/up rebuild_archive</code> — 从 NAS 重建归档索引\n"
                "<code>/up clear_queue</code> — 清空下载队列（不中断当前下载）\n"
                "<code>/up check</code> — 立即检查\n"
                "<code>/up sync</code> — 同步本地文件到 NAS\n"
                "<code>/up pause</code> — 暂停监控\n"
                "<code>/up resume</code> — 恢复监控\n"
                "<code>/up queue</code> — 查看下载队列\n"
                "<code>/up history [N]</code> — 下载历史"
            )

        if result and self._client:
            self._client.send_message(result, parse_mode="HTML")
        return None

    def _cmd_status(self) -> str:
        with self._state_lock:
            ups = len(self._state["monitored_ups"])
            downloaded = len(self._state["downloaded_bvids"])
            history = self._state["download_history"]
            last_ok = history[-1]["downloaded_at"] if history else "N/A"

        status = "暂停" if self._paused.is_set() else "运行中"
        pending = self._queue.qsize()
        cur = self._current_download
        cur_text = f"\n当前下载: {cur['title']}" if cur else ""

        return (
            f"<b>B站UP主监控</b>\n"
            f"状态: {status}\n"
            f"监控UP主: {ups}\n"
            f"已记录视频: {downloaded}\n"
            f"队列等待: {pending}{cur_text}\n"
            f"检查间隔: {self._check_interval}s\n"
            f"上次记录: {last_ok}"
        )

    def _cmd_list(self) -> str:
        with self._state_lock:
            ups = self._state["monitored_ups"]
        if not ups:
            return "当前没有监控任何UP主。使用 <code>/up add &lt;UID&gt;</code> 添加。"
        lines = ["<b>监控中的UP主</b>\n"]
        for mid, info in ups.items():
            mode = "自动下载" if not info.get("notify_only", True) else "仅通知"
            lines.append(
                f"  <code>{mid}</code> — {html.escape(info['name'])} [{mode}]"
            )
        return "\n".join(lines)

    def _cmd_add(self, arg: str) -> str:
        # Parse --download flag
        parts = arg.split()
        if not parts:
            return "用法: <code>/up add &lt;UID&gt; [--download]</code>"
        mid_str = parts[0]
        notify_only = "--download" not in parts

        if not mid_str.isdigit():
            return "UID 必须是数字。"

        with self._state_lock:
            if mid_str in self._state["monitored_ups"]:
                return f"UP主 {mid_str} 已在监控中。"

        # Get UP info
        up_info = self._api_get_up_info(int(mid_str))
        if not up_info:
            return f"无法获取 UID {mid_str} 的UP主信息，请确认 UID 是否正确。"

        up_name = up_info["name"]

        # Fetch first page of videos to get last_check_aid and seed known bvids
        # Retry up to 3 times on failure (412 etc.)
        videos = []
        for attempt in range(3):
            videos = self._api_fetch_up_videos(int(mid_str), pn=1, ps=30)
            if videos:
                break
            time.sleep(1)

        last_aid = 0
        if videos:
            last_aid = max(v.get("aid", 0) for v in videos)

        if not videos:
            logger.warning("Failed to fetch videos for UP %s after retries, adding with last_check_aid=0", mid_str)

        with self._state_lock:
            self._state["monitored_ups"][mid_str] = {
                "name": up_name,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "notify_only": notify_only,
                "last_check_aid": last_aid,
            }
            # Seed existing videos as known
            known = set(self._state["downloaded_bvids"])
            seeded = 0
            for v in videos or []:
                bvid = v.get("bvid", "")
                if bvid and bvid not in known:
                    self._state["downloaded_bvids"].append(bvid)
                    known.add(bvid)
                    seeded += 1
            self._trim_bvids()
            self._save_state()

        mode_text = "自动下载" if not notify_only else "仅通知"
        warn = ""
        if not videos:
            warn = "\n⚠ 首次获取视频列表失败，可能在下次检查时发送大量历史通知。建议 /up remove 后重试。"
        return (
            f"已添加UP主监控: <b>{html.escape(up_name)}</b> (UID: {mid_str})\n"
            f"模式: {mode_text}\n"
            f"已标记 {seeded} 个现有视频{warn}"
        )

    def _cmd_remove(self, arg: str) -> str:
        mid_str = arg.strip()
        with self._state_lock:
            if mid_str not in self._state["monitored_ups"]:
                return f"UP主 {mid_str} 不在监控列表中。"
            name = self._state["monitored_ups"].pop(mid_str)["name"]
            self._save_state()
        return f"已移除UP主监控: <b>{html.escape(name)}</b> (UID: {mid_str})"

    def _cmd_mode(self, arg: str) -> str:
        parts = arg.strip().split()
        if len(parts) != 2 or parts[1] not in ("notify", "download"):
            return "用法: <code>/up mode &lt;UID&gt; notify/download</code>"
        mid_str, mode = parts

        with self._state_lock:
            if mid_str not in self._state["monitored_ups"]:
                return f"UP主 {mid_str} 不在监控列表中。"
            self._state["monitored_ups"][mid_str]["notify_only"] = (mode == "notify")
            name = self._state["monitored_ups"][mid_str]["name"]
            self._save_state()

        mode_text = "仅通知" if mode == "notify" else "自动下载"
        return f"UP主 <b>{html.escape(name)}</b> 已切换为: {mode_text}"

    def _cmd_download(self, arg: str) -> str:
        # Parse --force flag
        parts = arg.strip().split()
        force = "--force" in parts
        parts = [p for p in parts if p != "--force"]
        if not parts or not parts[0].isdigit():
            return "用法: <code>/up download &lt;UID&gt; [--force]</code>\n默认只下载缺失的视频；<code>--force</code> 强制重新下载全部。"
        mid_str = parts[0]

        # Get UP name
        with self._state_lock:
            info = self._state["monitored_ups"].get(mid_str)
        if info:
            up_name = info["name"]
        else:
            up_info = self._api_get_up_info(int(mid_str))
            if not up_info:
                return f"无法获取 UID {mid_str} 的UP主信息。"
            up_name = up_info["name"]

        # Notify user that we're fetching
        if self._client:
            self._client.send_message(
                f"正在获取UP主 <b>{html.escape(up_name)}</b> 的视频列表...",
                parse_mode="HTML",
            )

        # Fetch ALL videos
        all_videos, complete = self._api_fetch_all_up_videos(int(mid_str))
        if not all_videos:
            return "该UP主没有可下载的视频，或 API 请求失败（可稍后重试）。"

        # Collect bvids already in queue to avoid duplicates
        with self._queue.mutex:
            queued_bvids = {item["bvid"] for item in self._queue.queue}
        cur = self._current_download
        if cur:
            queued_bvids.add(cur["bvid"])

        count = 0
        skipped_downloaded = 0
        skipped_archived = 0
        with self._state_lock:
            known = set(self._state["downloaded_bvids"])
            for v in reversed(all_videos):  # oldest first into queue
                bvid = v.get("bvid", "")
                if not bvid or bvid in queued_bvids:
                    continue
                # Archive check (fast in-memory lookup; NAS verification still done at download time)
                if not force and self._archive is not None and self._archive.has(bvid):
                    skipped_archived += 1
                    continue
                if bvid in known:
                    if force:
                        # Remove from known so downloader will re-process it
                        try:
                            self._state["downloaded_bvids"].remove(bvid)
                        except ValueError:
                            pass
                        known.discard(bvid)
                    else:
                        # Default: skip already-downloaded
                        skipped_downloaded += 1
                        continue
                task = {
                    "bvid": bvid,
                    "title": v.get("title", bvid),
                    "up_mid": mid_str,
                    "up_name": up_name,
                }
                self._queue.put(task)
                self._state["pending_queue"].append(task)
                count += 1
            self._save_state()

        warn = ""
        if not complete:
            warn = "\n⚠ 分页中断（B站限流），未获取完整列表。稍后请再次运行此命令补齐。"
        mode_text = "强制重下" if force else "只下载缺失"
        skipped_queue = len(all_videos) - count - skipped_downloaded - skipped_archived
        return (
            f"UP主 <b>{html.escape(up_name)}</b> 全量下载已启动（模式: {mode_text}）\n"
            f"有效视频: {len(all_videos)}，新加入队列: {count}\n"
            f"跳过（已归档）: {skipped_archived}，"
            f"跳过（已下载）: {skipped_downloaded}，"
            f"跳过（已在队列）: {skipped_queue}{warn}"
        )

    def _cmd_redo(self, arg: str) -> str:
        bvid = arg.strip()
        if not re.match(r'^BV[a-zA-Z0-9]+$', bvid):
            return "用法: <code>/up redo &lt;BV号&gt;</code>"

        # Determine UP info: prefer archive, then download_history
        up_mid = ""
        up_name = ""
        title = bvid
        arch = self._archive.get(bvid) if self._archive else None
        if arch and arch.get("source_type") == "up":
            up_mid = arch.get("source_id", "")
            up_name = arch.get("source_name", "")
            title = arch.get("title", bvid)
        else:
            with self._state_lock:
                for entry in reversed(self._state["download_history"]):
                    if entry.get("bvid") == bvid:
                        up_mid = entry.get("up_mid", "")
                        up_name = entry.get("up_name", "")
                        title = entry.get("title", bvid)
                        break

        if not up_name:
            return (
                f"BV {bvid} 未找到归档或历史记录，无法确定所属UP主。\n"
                f"如果该视频不属于监控的UP主，请使用 <code>/dl</code> 命令单独下载。"
            )

        # Remove from archive and downloaded_bvids to force re-download
        if self._archive:
            self._archive.remove(bvid)
        with self._state_lock:
            bvids = self._state["downloaded_bvids"]
            if bvid in bvids:
                bvids.remove(bvid)
            self._save_state()

        task = {
            "bvid": bvid,
            "title": title,
            "up_mid": up_mid,
            "up_name": up_name,
        }
        self._redo_queue.put(task)
        return (
            f"已加入强制重下快速通道: <b>{html.escape(title)}</b>\n"
            f"BV号: <code>{bvid}</code>\n"
            f"UP主: {html.escape(up_name)}"
        )

    def _cmd_rebuild_archive(self) -> str:
        if self._archive is None:
            return "归档未启用。"
        if not self._nas_enabled:
            return "NAS 同步未启用。无法从 NAS 扫描重建归档。"

        # SSH find to list all video files under nas_dest_dir
        find_cmd = (
            f"find {shlex.quote(self._nas_dest_dir)} -type f "
            r"\( -name '*.mp4' -o -name '*.mkv' -o -name '*.webm' "
            r"-o -name '*.flv' -o -name '*.avi' \)"
        )
        try:
            result = subprocess.run(
                ["ssh", self._nas_host, find_cmd],
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            return f"NAS 扫描失败: {html.escape(str(e))}"

        if result.returncode != 0:
            return f"NAS 扫描失败: {html.escape((result.stderr or 'unknown')[-200:])}"

        # Build UP name → mid lookup (so we can tag source_type accurately)
        with self._state_lock:
            up_name_to_mid = {info["name"]: mid for mid, info in self._state["monitored_ups"].items()}

        bv_pattern = re.compile(r'\[(BV[a-zA-Z0-9]+)\]')
        title_pattern = re.compile(r'^(.+?)\s*\[BV[a-zA-Z0-9]+\]')

        added = 0
        updated = 0
        skipped_nobv = 0

        for line in result.stdout.splitlines():
            path = line.strip()
            if not path:
                continue
            m = bv_pattern.search(path)
            if not m:
                skipped_nobv += 1
                continue
            bvid = m.group(1)

            # Parent folder under nas_dest_dir
            rel = path
            if rel.startswith(self._nas_dest_dir.rstrip("/") + "/"):
                rel = rel[len(self._nas_dest_dir.rstrip("/")) + 1:]
            parts = rel.split("/", 1)
            folder = parts[0] if len(parts) >= 2 else ""

            filename = path.rsplit("/", 1)[-1]
            tm = title_pattern.match(filename)
            title = tm.group(1).strip() if tm else filename

            source_type = "up" if folder in up_name_to_mid else "unknown"
            source_id = up_name_to_mid.get(folder, "")

            if self._archive.has(bvid):
                updated += 1
            else:
                added += 1

            self._archive.add(bvid, {
                "path": path,
                "title": title,
                "source_type": source_type,
                "source_id": source_id,
                "source_name": folder,
                "on_nas": True,
            })

        return (
            f"<b>NAS 归档重建完成</b>\n"
            f"扫描路径: <code>{html.escape(self._nas_dest_dir)}</code>\n"
            f"新增: {added}，更新: {updated}，无 BV 号跳过: {skipped_nobv}\n"
            f"归档总数: {self._archive.count()}"
        )

    def _cmd_clear_queue(self) -> str:
        # Drain in-memory main queue (redo queue preserved)
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                drained += 1
            except queue.Empty:
                break
        # Clear persistent pending_queue
        with self._state_lock:
            persisted = len(self._state["pending_queue"])
            self._state["pending_queue"] = []
            self._save_state()
        cur = self._current_download
        note = ""
        if cur:
            note = f"\n当前正在下载的视频不会中断：{html.escape(cur.get('title', ''))}"
        return (
            f"<b>已清空 UP主 下载队列</b>\n"
            f"内存队列: 取消 {drained} 个\n"
            f"持久化队列: 清除 {persisted} 个\n"
            f"快速通道（redo）未受影响。{note}"
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
        with self._state_lock:
            self._state["paused"] = True
            self._save_state()
        return "UP主监控已暂停。使用 <code>/up resume</code> 恢复。"

    def _cmd_resume(self) -> str:
        self._paused.clear()
        with self._state_lock:
            self._state["paused"] = False
            self._save_state()
        return "UP主监控已恢复。"

    def _cmd_queue(self) -> str:
        cur = self._current_download
        activity = self._current_activity
        with self._queue.mutex:
            pending = list(self._queue.queue)
        with self._redo_queue.mutex:
            redo_pending = list(self._redo_queue.queue)

        lines = ["<b>下载队列</b>\n"]
        if cur:
            lines.append(f"正在下载:\n  {html.escape(cur['title'])} (<code>{cur['bvid']}</code>)")
        elif activity == "startup_sync":
            lines.append("当前状态: 启动时 NAS 同步中（同步完成后才会开始下载）")
        elif activity == "nas_syncing":
            lines.append("当前状态: NAS 批量同步中")
        else:
            lines.append("正在下载: 无")

        if redo_pending:
            lines.append(f"\n快速通道 ({len(redo_pending)}):")
            for i, item in enumerate(redo_pending[:10], 1):
                lines.append(f"  {i}. {html.escape(item['title'][:50])} (<code>{item['bvid']}</code>)")

        if pending:
            lines.append(f"\n等待中 ({len(pending)}):")
            for i, item in enumerate(pending[:20], 1):
                if item.get("_action"):
                    lines.append(f"  {i}. [系统任务: {item['_action']}]")
                else:
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
        logger.info("UP monitor thread started")
        # Initial delay to avoid colliding with other startup API calls (cookie validation etc.)
        self._shutdown_event.wait(timeout=10)
        retries = 0
        while not self._shutdown_event.is_set():
            # Pause check
            while self._paused.is_set() and not self._shutdown_event.is_set():
                self._shutdown_event.wait(timeout=5)
            if self._shutdown_event.is_set():
                break

            try:
                new_count = self._check_all_ups()
                retries = 0
                debug_bus.emit("bilibili_up_check", {
                    "ups": len(self._state["monitored_ups"]),
                    "new_videos": new_count,
                })
            except Exception as e:
                retries += 1
                backoff = min(30 * (2 ** retries), 600)
                logger.warning("UP check error (retry %d, backoff %ds): %s", retries, backoff, e)
                debug_bus.emit("bilibili_up_error", {"error": str(e)})
                self._shutdown_event.wait(timeout=backoff)
                continue

            # Interruptible sleep
            if self._check_now_event.is_set():
                self._check_now_event.clear()
            else:
                self._check_now_event.wait(timeout=self._check_interval)
                self._check_now_event.clear()

        logger.info("UP monitor thread stopped")

    def _check_all_ups(self) -> int:
        """Check all monitored UPs for new videos. Returns count of new items found."""
        if not self._cookies_path:
            return 0

        # Cookie validation
        if not check_cookie_valid(self._cookies_path):
            logger.warning("Bilibili cookie invalid, skipping UP check")
            return 0

        with self._state_lock:
            ups = dict(self._state["monitored_ups"])

        if not ups:
            return 0

        total_new = 0
        for mid_str, info in ups.items():
            if self._shutdown_event.is_set():
                break
            new = self._check_single_up(mid_str, info)
            total_new += new
            # Rate limit between UP checks
            if not self._shutdown_event.is_set():
                time.sleep(1)

        return total_new

    # Max individual notifications before switching to batch summary
    _NOTIFY_BATCH_THRESHOLD = 5

    def _check_single_up(self, mid_str: str, info: dict) -> int:
        """Check one UP for new videos. Returns count of new items found."""
        videos = self._api_fetch_up_videos(int(mid_str), pn=1, ps=30)
        if not videos:
            return 0

        last_aid = info.get("last_check_aid", 0)
        new_videos = [v for v in videos if v.get("aid", 0) > last_aid]

        if not new_videos:
            return 0

        # Update last_check_aid to newest
        max_aid = max(v["aid"] for v in new_videos)

        notify_only = info.get("notify_only", True)
        up_name = info["name"]

        # Collect truly new videos
        collected = []
        with self._state_lock:
            self._state["monitored_ups"][mid_str]["last_check_aid"] = max_aid

            known = set(self._state["downloaded_bvids"])

            for video in sorted(new_videos, key=lambda v: v["aid"]):
                bvid = video.get("bvid", "")
                if not bvid or bvid in known:
                    continue

                collected.append(video)

                if not notify_only:
                    task = {
                        "bvid": bvid,
                        "title": video.get("title", bvid),
                        "up_mid": mid_str,
                        "up_name": up_name,
                    }
                    self._queue.put(task)
                    self._state["pending_queue"].append(task)

                # In-loop dedup only; do NOT persist to downloaded_bvids here.
                # For auto-download: the downloader will add it after success.
                # For notify-only: last_check_aid prevents re-detection next cycle.
                known.add(bvid)

            self._save_state()

        # Send notifications (batch if too many)
        count = len(collected)
        if count > 0:
            if count <= self._NOTIFY_BATCH_THRESHOLD:
                for video in collected:
                    self._notify_new_video(up_name, mid_str, video)
                    time.sleep(0.3)  # throttle to avoid Telegram rate limit
            else:
                self._notify_new_videos_batch(up_name, mid_str, collected, notify_only)
            logger.info("Found %d new videos from UP %s (%s)", count, mid_str, up_name)
        return count

    # ------------------------------------------------------------------
    # Downloader thread
    # ------------------------------------------------------------------

    def _downloader_thread(self):
        logger.info("UP downloader thread started")

        # Sync any previously downloaded but unsynced files on startup
        if self._nas_enabled:
            self._current_activity = "startup_sync"
            try:
                self._sync_all_pending()
            except Exception as e:
                logger.warning("Startup NAS sync error: %s", e)
            self._current_activity = "idle"

        while not self._shutdown_event.is_set():
            # Prefer fast-track (redo) tasks before the main queue
            task = None
            is_redo = False
            try:
                task = self._redo_queue.get_nowait()
                is_redo = True
            except queue.Empty:
                try:
                    task = self._queue.get(timeout=5)
                except queue.Empty:
                    continue

            # Handle sentinel tasks
            if task.get("_action") == "sync_all":
                self._current_activity = "nas_syncing"
                try:
                    self._sync_all_pending()
                except Exception as e:
                    logger.warning("NAS sync error: %s", e)
                self._current_activity = "idle"
                if is_redo:
                    self._redo_queue.task_done()
                else:
                    self._queue.task_done()
                continue

            bvid = task.get("bvid", "")

            # Archive check: if already archived AND file verified on NAS, skip.
            # Redo tasks bypass the archive check (they intentionally want re-download).
            if not is_redo and self._archive is not None:
                arch = self._archive.get(bvid)
                if arch:
                    on_nas = arch.get("on_nas", False)
                    path = arch.get("path", "")
                    verified = False
                    if on_nas and path and self._nas_enabled:
                        verified = verify_nas_file(self._nas_host, path)
                    elif not on_nas and path:
                        verified = Path(path).exists()
                    if verified:
                        logger.info("Archived already, skipping: %s (%s)", bvid, path)
                        with self._state_lock:
                            pq = self._state["pending_queue"]
                            for i, item in enumerate(pq):
                                if item.get("bvid") == bvid:
                                    pq.pop(i)
                                    break
                            # Also mark in downloaded_bvids so future checks are cheap
                            if bvid not in set(self._state["downloaded_bvids"]):
                                self._state["downloaded_bvids"].append(bvid)
                                self._trim_bvids()
                            self._save_state()
                        self._queue.task_done()
                        continue
                    else:
                        # Archive says we have it, but file is missing — drop archive entry and re-download
                        logger.warning("Archive entry for %s points to missing file, re-downloading", bvid)
                        self._archive.remove(bvid)

            # Skip if already downloaded (prevents re-download after crash during record).
            # Redo tasks bypass this too (downloaded_bvids already cleared by _cmd_redo).
            if not is_redo:
                with self._state_lock:
                    already = bvid in set(self._state["downloaded_bvids"])
                if already:
                    with self._state_lock:
                        pq = self._state["pending_queue"]
                        for i, item in enumerate(pq):
                            if item.get("bvid") == bvid:
                                pq.pop(i)
                                break
                        self._save_state()
                    self._queue.task_done()
                    continue

            self._current_download = task
            self._current_activity = "downloading"
            try:
                self._download_video(task)
            except Exception as e:
                logger.exception("Download failed for %s: %s", task["bvid"], e)
                self._record_history(task, "failed", str(e))
                self._notify_download_failure(task, str(e))
            finally:
                # Redo tasks are not tracked in persistent pending_queue;
                # regular tasks are removed here so mid-download restarts re-queue them.
                if not is_redo:
                    with self._state_lock:
                        pq = self._state["pending_queue"]
                        for i, item in enumerate(pq):
                            if item.get("bvid") == task.get("bvid"):
                                pq.pop(i)
                                break
                        self._save_state()
                self._current_download = None
                self._current_activity = "idle"
                if is_redo:
                    self._redo_queue.task_done()
                else:
                    self._queue.task_done()

        logger.info("UP downloader thread stopped")

    def _download_video(self, task: dict):
        bvid = task["bvid"]
        title = task["title"]
        up_name = task["up_name"]

        url = f"https://www.bilibili.com/video/{bvid}"

        # Create per-UP subdirectory
        safe_folder = re.sub(r'[\\/:*?"<>|]', '_', up_name).strip() or "default"
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
        nas_ok = False
        if self._nas_enabled and filepath:
            nas_ok = self._sync_to_nas(filepath, safe_folder)
            nas_status = "\nNAS: 已同步" if nas_ok else "\nNAS: 同步失败"

        # Archive entry — record final path (NAS if synced, else local)
        if self._archive is not None and filepath:
            final_path = filepath
            if self._nas_enabled and nas_ok:
                final_path = f"{self._nas_dest_dir}/{safe_folder}/{Path(filepath).name}"
            self._archive.add(bvid, {
                "path": final_path,
                "title": title,
                "source_type": "up",
                "source_id": task.get("up_mid", ""),
                "source_name": up_name,
                "on_nas": bool(self._nas_enabled and nas_ok),
            })

        self._record_history(task, "success")
        self._notify_download_success(task, filepath, nas_status)
        logger.info("Downloaded %s: %s -> %s", bvid, title, filepath)
        debug_bus.emit("bilibili_up_download", {
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
            msg = f"<b>[NAS 同步 - UP主]</b> 完成: 成功 {synced} 个"
            if failed:
                msg += f"，失败 {failed} 个"
            logger.info("NAS sync all (UP): synced=%d, failed=%d", synced, failed)
            if self._client:
                self._client.send_message(msg, parse_mode="HTML")

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def _notify_new_video(self, up_name: str, mid_str: str, video: dict):
        """Send notification for a newly detected video."""
        bvid = video.get("bvid", "")
        title = video.get("title", "")
        created = video.get("created", 0)
        pub_time = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M") if created else "未知"
        link = f"https://www.bilibili.com/video/{bvid}"

        msg = (
            f"<b>[UP主更新]</b>\n\n"
            f"UP主: {html.escape(up_name)} (UID: {mid_str})\n"
            f"标题: {html.escape(title)}\n"
            f"BV号: <code>{bvid}</code>\n"
            f"链接: {link}\n"
            f"发布时间: {pub_time}"
        )
        if self._client:
            self._client.send_message(msg, parse_mode="HTML")

    def _notify_new_videos_batch(self, up_name: str, mid_str: str, videos: list[dict], notify_only: bool):
        """Send a single batch notification when many new videos are detected."""
        mode_text = "仅通知" if notify_only else "自动下载"
        lines = [
            f"<b>[UP主更新]</b> {html.escape(up_name)} (UID: {mid_str})\n",
            f"检测到 {len(videos)} 个新视频（模式: {mode_text}）\n",
        ]
        # Show up to 10 titles
        for i, v in enumerate(videos[:10], 1):
            bvid = v.get("bvid", "")
            title = v.get("title", "")
            lines.append(f"  {i}. {html.escape(title[:50])} (<code>{bvid}</code>)")
        if len(videos) > 10:
            lines.append(f"  ... 还有 {len(videos) - 10} 个")

        if not notify_only:
            lines.append(f"\n已全部加入下载队列。")

        msg = "\n".join(lines)
        if self._client:
            self._client.send_message(msg, parse_mode="HTML")

    def _notify_download_success(self, task: dict, filepath: str | None, nas_status: str = ""):
        path_line = f"\n路径: <code>{html.escape(str(filepath))}</code>" if filepath else ""
        msg = (
            f"<b>[UP主视频下载]</b>\n\n"
            f"UP主: {html.escape(task['up_name'])}\n"
            f"标题: {html.escape(task['title'])}\n"
            f"BV号: <code>{task['bvid']}</code>\n"
            f"状态: 下载完成"
            f"{path_line}{nas_status}"
        )
        if self._client:
            self._client.send_message(msg, parse_mode="HTML")

    def _notify_download_failure(self, task: dict, error: str):
        msg = (
            f"<b>[UP主视频下载]</b>\n\n"
            f"UP主: {html.escape(task['up_name'])}\n"
            f"标题: {html.escape(task['title'])}\n"
            f"BV号: <code>{task['bvid']}</code>\n"
            f"状态: 下载失败\n"
            f"原因: {html.escape(error[:200])}"
        )
        if self._client:
            self._client.send_message(msg, parse_mode="HTML")
        debug_bus.emit("bilibili_up_download", {
            "bvid": task["bvid"], "title": task["title"], "status": "failed",
        })

    def _record_history(self, task: dict, status: str, error: str = ""):
        entry = {
            "bvid": task["bvid"],
            "title": task["title"],
            "up_mid": task["up_mid"],
            "up_name": task["up_name"],
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
        }
        if error:
            entry["error"] = error[:200]

        with self._state_lock:
            self._state["download_history"].append(entry)
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

    def _build_cookie_header(self) -> str:
        if not self._cookies_path:
            return ""
        sessdata = _parse_cookie_value(self._cookies_path, "SESSDATA")
        return f"SESSDATA={sessdata}" if sessdata else ""

    def _api_get_up_info(self, mid: int) -> dict | None:
        """Get UP name from card API (no WBI needed)."""
        cookie = self._build_cookie_header()
        try:
            req = urllib.request.Request(
                f"https://api.bilibili.com/x/web-interface/card?mid={mid}",
                headers={"User-Agent": USER_AGENT, "Cookie": cookie},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            if data.get("code") == 0 and data.get("data"):
                card = data["data"].get("card", {})
                name = card.get("name", "")
                if name:
                    return {"mid": mid, "name": name}
        except Exception as e:
            logger.warning("Failed to get UP info for mid=%d: %s", mid, e)
        return None

    def _get_wbi_mixin_key(self) -> str | None:
        """Get WBI mixin key, cached for 12 hours."""
        now = time.time()
        if self._wbi_mixin_key and (now - self._wbi_key_ts) < 43200:
            return self._wbi_mixin_key

        cookie = self._build_cookie_header()
        try:
            req = urllib.request.Request(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={"User-Agent": USER_AGENT, "Cookie": cookie},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read().decode())
            if data.get("code") != 0:
                return None

            wbi_img = data.get("data", {}).get("wbi_img", {})
            img_url = wbi_img.get("img_url", "")
            sub_url = wbi_img.get("sub_url", "")

            if not img_url or not sub_url:
                return None

            # Extract key from URL: last path segment without extension
            img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]

            raw = img_key + sub_key
            if len(raw) < 64:
                logger.warning("WBI key raw string too short: %d", len(raw))
                return None

            mixin_key = "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
            self._wbi_mixin_key = mixin_key
            self._wbi_key_ts = now
            logger.info("WBI mixin key refreshed")
            return mixin_key

        except Exception as e:
            logger.warning("Failed to get WBI keys: %s", e)
            return None

    def _sign_wbi(self, params: dict) -> dict:
        """Add WBI signature (w_rid, wts) to params dict."""
        mixin_key = self._get_wbi_mixin_key()
        if not mixin_key:
            return params  # fallback: try unsigned

        params = dict(params)
        params["wts"] = int(time.time())

        # Filter special chars from values and sort by key
        filtered = {}
        for k in sorted(params.keys()):
            v = str(params[k])
            filtered[k] = "".join(c for c in v if c not in "!'()*")

        query = urllib.parse.urlencode(filtered)
        w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
        filtered["w_rid"] = w_rid
        return filtered

    def _api_fetch_up_videos(self, mid: int, pn: int = 1, ps: int = 30, raise_on_error: bool = False) -> list[dict]:
        """Fetch one page of an UP's video list (sorted by pubdate, newest first).

        Retries once on 412 (rate limit) with a short backoff.
        If raise_on_error is True, raises RuntimeError on failure instead of returning []
        (used by pagination to distinguish "end of list" from "request failed").
        """
        cookie = self._build_cookie_header()

        for attempt in range(2):
            params = {
                "mid": mid,
                "ps": ps,
                "pn": pn,
                "order": "pubdate",
            }
            signed_params = self._sign_wbi(params)
            query = urllib.parse.urlencode(signed_params)
            url = f"https://api.bilibili.com/x/space/wbi/arc/search?{query}"

            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Cookie": cookie,
                        "Referer": f"https://space.bilibili.com/{mid}/video",
                    },
                )
                resp = urllib.request.urlopen(req, timeout=15)
                data = json.loads(resp.read().decode())
                if data.get("code") == 0 and data.get("data"):
                    vlist = data["data"].get("list", {}).get("vlist", [])
                    return vlist
                else:
                    logger.warning("UP video API returned code=%s for mid=%d", data.get("code"), mid)
                    if raise_on_error:
                        raise RuntimeError(f"API returned code={data.get('code')}")
                    return []
            except urllib.error.HTTPError as e:
                if e.code == 412 and attempt == 0:
                    logger.info("Got 412 for mid=%d, retrying after 2s...", mid)
                    time.sleep(2)
                    continue
                logger.warning("Failed to fetch UP videos (mid=%d, pn=%d): %s", mid, pn, e)
                if raise_on_error:
                    raise RuntimeError(f"HTTP {e.code}") from e
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning("Failed to fetch UP videos (mid=%d, pn=%d): %s", mid, pn, e)
                if raise_on_error:
                    raise RuntimeError(str(e)) from e
                break
        return []

    def _api_fetch_up_videos_with_retry(self, mid: int, pn: int, ps: int = 30,
                                         max_retries: int = 5) -> list[dict]:
        """Fetch one page with aggressive retry on failure. Used by full-download pagination.

        Returns [] only when the page is truly empty (end of list).
        Raises RuntimeError after exhausting retries.
        """
        backoff = 5
        for attempt in range(max_retries):
            try:
                videos = self._api_fetch_up_videos(mid, pn=pn, ps=ps, raise_on_error=True)
                return videos
            except RuntimeError as e:
                if attempt == max_retries - 1:
                    raise
                logger.info("Page fetch failed (mid=%d pn=%d attempt=%d): %s, retrying in %ds",
                            mid, pn, attempt + 1, e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        return []

    def _api_fetch_all_up_videos(self, mid: int) -> tuple[list[dict], bool]:
        """Fetch ALL videos from an UP (paginated). Used for full download.

        Returns (videos, complete) where complete=True only if pagination
        finished cleanly. complete=False means some pages failed even after retries
        (partial list returned).
        """
        all_videos = []
        pn = 1
        complete = True
        while True:
            if self._shutdown_event.is_set():
                complete = False
                break
            try:
                videos = self._api_fetch_up_videos_with_retry(mid, pn=pn, ps=30)
            except RuntimeError as e:
                logger.error("Pagination failed at page %d for mid=%d: %s", pn, mid, e)
                complete = False
                break
            if not videos:
                # True end of list (empty response)
                break
            all_videos.extend(videos)
            if len(videos) < 30:
                break
            pn += 1
            # Rate limit courtesy
            time.sleep(1)
        return all_videos, complete

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        if self._state_path.exists():
            try:
                with open(self._state_path) as f:
                    loaded = json.load(f)
                self._state.update(loaded)
                self._state.setdefault("monitored_ups", {})
                self._state.setdefault("downloaded_bvids", [])
                self._state.setdefault("download_history", [])
                self._state.setdefault("pending_queue", [])
            except Exception as e:
                logger.warning("Failed to load UP monitor state: %s", e)

    def _save_state(self):
        """Atomic write state to disk. Caller must hold _state_lock."""
        tmp = self._state_path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            tmp.replace(self._state_path)
        except Exception as e:
            logger.warning("Failed to save UP monitor state: %s", e)
            if tmp.exists():
                tmp.unlink()

    def _trim_bvids(self):
        """Trim downloaded_bvids to rolling window cap. Caller must hold _state_lock."""
        bvids = self._state["downloaded_bvids"]
        if len(bvids) > _MAX_DOWNLOADED_BVIDS:
            self._state["downloaded_bvids"] = bvids[-_MAX_DOWNLOADED_BVIDS:]
