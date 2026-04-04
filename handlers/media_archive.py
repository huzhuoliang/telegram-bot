"""Media archive handlers for saving and browsing incoming media files."""

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class MediaArchiveHandler:
    """Saves incoming photos and videos forwarded to the bot."""

    _index_lock = threading.Lock()

    def __init__(self, archive_dir: str, telegram_client):
        self.archive_dir = Path(archive_dir).expanduser()
        self._client = telegram_client

    def _append_index(self, file_id: str, media_type: str, rel_path: str, ts: str):
        index_path = self.archive_dir / "archive_index.json"
        tmp_path = self.archive_dir / "archive_index.json.tmp"
        with MediaArchiveHandler._index_lock:
            try:
                entries = json.loads(index_path.read_text()).get("entries", []) if index_path.exists() else []
            except Exception:
                entries = []
            entries.append({"type": media_type, "file_id": file_id, "rel_path": rel_path, "ts": ts})
            tmp_path.write_text(json.dumps({"entries": entries}))
            os.replace(tmp_path, index_path)

    def handle(self, message: dict) -> str:
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ts_readable = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if "photo" in message:
            # Telegram sends multiple sizes; pick the largest (last in array)
            file_id = message["photo"][-1]["file_id"]
            save_path = str(self.archive_dir / "photos" / f"{ts}.jpg")
            kind = "图片"
            kind_key = "photo"
        elif "video" in message:
            file_id = message["video"]["file_id"]
            ext = message["video"].get("mime_type", "video/mp4").split("/")[-1]
            save_path = str(self.archive_dir / "videos" / f"{ts}.{ext}")
            kind = "视频"
            kind_key = "video"
        elif "document" in message:
            doc = message["document"]
            file_id = doc["file_id"]
            filename = doc.get("file_name", f"{ts}.bin")
            save_path = str(self.archive_dir / "documents" / filename)
            kind = "文件"
            kind_key = "document"
        else:
            return "不支持的媒体类型。"

        logger.info("Archiving %s → %s", kind, save_path)
        ok = self._client.download_file(file_id, save_path)
        if ok:
            rel = str(Path(save_path).relative_to(self.archive_dir))
            self._append_index(file_id, kind_key, rel, ts_readable)
            return f"✅ {kind}已存档：{save_path}"
        else:
            return f"❌ {kind}存档失败，请查看日志。"


class FileArchiveHandler:
    """Browse, preview and download archived media files via inline keyboard."""

    PAGE_SIZE = 8
    TYPE_LABELS = {"photo": "📷 照片", "video": "📹 视频", "document": "📄 文档"}

    def __init__(self, archive_dir: str, telegram_client):
        self.archive_dir = Path(archive_dir).expanduser()
        self._client = telegram_client

    def _load_index(self) -> list:
        path = self.archive_dir / "archive_index.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text()).get("entries", [])
        except Exception:
            return []

    def _main_menu_markup(self, entries: list) -> dict:
        counts = {t: sum(1 for e in entries if e["type"] == t) for t in ("photo", "video", "document")}
        buttons = [
            {"text": f"📷 照片 ({counts['photo']})", "callback_data": "files:photo:0"},
            {"text": f"📹 视频 ({counts['video']})", "callback_data": "files:video:0"},
            {"text": f"📄 文档 ({counts['document']})", "callback_data": "files:document:0"},
        ]
        return {"inline_keyboard": [buttons]}

    def handle_command(self):
        """Handle /files command — sends the main menu."""
        entries = self._load_index()
        markup = self._main_menu_markup(entries)
        self._client.send_message_with_keyboard("📁 归档文件", markup)

    def handle_callback(self, callback_query: dict):
        """Handle all files:* and file:* callback queries."""
        cq_id = callback_query["id"]
        data = callback_query.get("data", "")
        message_id = callback_query.get("message", {}).get("message_id")

        # Answer immediately to dismiss the loading spinner
        self._client.answer_callback_query(cq_id)

        parts = data.split(":", 2)
        if not parts:
            return

        if parts[0] == "files":
            if len(parts) == 2 and parts[1] == "menu":
                entries = self._load_index()
                markup = self._main_menu_markup(entries)
                self._client.edit_message_text(message_id, "📁 归档文件", reply_markup=markup)
            elif len(parts) == 3:
                media_type = parts[1]
                try:
                    page = int(parts[2])
                except ValueError:
                    return
                self._show_page(message_id, media_type, page)

        elif parts[0] == "file" and len(parts) == 3:
            media_type = parts[1]
            try:
                idx = int(parts[2])
            except ValueError:
                return
            entries = self._load_index()
            if 0 <= idx < len(entries) and entries[idx]["type"] == media_type:
                entry = entries[idx]
                _method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}.get(media_type)
                _field = {"photo": "photo", "video": "video", "document": "document"}.get(media_type)
                ok = False
                if _method and _field:
                    try:
                        self._client.call_api(_method, chat_id=self._client.chat_id, **{_field: entry["file_id"]})
                        ok = True
                    except Exception:
                        pass
                if not ok:
                    abs_path = str(self.archive_dir / entry["rel_path"])
                    if media_type == "photo":
                        self._client.send_photo(abs_path)
                    elif media_type == "video":
                        self._client.send_video(abs_path)

    def _show_page(self, message_id: int, media_type: str, page: int):
        entries = self._load_index()
        typed = [(i, e) for i, e in enumerate(entries) if e["type"] == media_type]
        typed.reverse()  # newest first
        total = len(typed)
        label = self.TYPE_LABELS.get(media_type, media_type)

        if total == 0:
            markup = {"inline_keyboard": [[{"text": "🔙 返回", "callback_data": "files:menu"}]]}
            self._client.edit_message_text(message_id, f"{label}\n暂无文件", reply_markup=markup)
            return

        total_pages = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        page = max(0, min(page, total_pages - 1))
        start = page * self.PAGE_SIZE
        chunk = typed[start:start + self.PAGE_SIZE]

        rows = []
        for orig_idx, entry in chunk:
            rows.append([{"text": entry["ts"], "callback_data": f"file:{media_type}:{orig_idx}"}])

        nav = []
        if page > 0:
            nav.append({"text": "◀ 上一页", "callback_data": f"files:{media_type}:{page - 1}"})
        if page < total_pages - 1:
            nav.append({"text": "下一页 ▶", "callback_data": f"files:{media_type}:{page + 1}"})
        nav.append({"text": "🔙 返回", "callback_data": "files:menu"})
        rows.append(nav)

        text = f"{label} ({total})  第 {page + 1}/{total_pages} 页"
        markup = {"inline_keyboard": rows}
        self._client.edit_message_text(message_id, text, reply_markup=markup)
