"""Thin wrapper around the Telegram Bot HTTP API."""

import logging
import requests
import debug_bus

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LEN = 4096

logger = logging.getLogger(__name__)


class TelegramClient:
    def __init__(self, token: str, chat_id: str, proxy: str = ""):
        self.token = token.strip()
        self.chat_id = str(chat_id).strip()
        self._session = requests.Session()
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}
            logger.info("Using proxy: %s", proxy)

    def _url(self, method: str) -> str:
        return TELEGRAM_API.format(token=self.token, method=method)

    def send_message(self, text: str, parse_mode: str = "", reply_to_message_id: int | None = None) -> int | None:
        """Send text to the configured chat. Splits messages > 4096 chars.
        Returns the message_id of the first chunk on success, None on failure."""
        if not text:
            return None

        chunks = [text[i:i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)]
        first_id = None
        for chunk in chunks:
            payload = {"chat_id": self.chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_to_message_id:
                payload["reply_to_message_id"] = reply_to_message_id
                reply_to_message_id = None  # only reply on first chunk
            try:
                resp = self._session.post(self._url("sendMessage"), json=payload, timeout=10)
                debug_bus.emit("telegram_out", {"method": "sendMessage", "payload": payload, "status": resp.status_code})
                if resp.ok:
                    if first_id is None:
                        first_id = resp.json().get("result", {}).get("message_id")
                else:
                    logger.warning("sendMessage failed: %s %s", resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning("sendMessage exception: %s", e)
        return first_id

    def delete_message(self, message_id: int) -> bool:
        """Delete a message by ID. Returns True on success."""
        try:
            resp = self._session.post(
                self._url("deleteMessage"),
                json={"chat_id": self.chat_id, "message_id": message_id},
                timeout=10,
            )
            return resp.ok
        except Exception as e:
            logger.warning("deleteMessage exception: %s", e)
            return False

    def _download_and_upload_photo(self, url: str, payload: dict) -> bool:
        """Fallback: download image via local proxy, upload as file to Telegram."""
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}
            dl = self._session.get(url, timeout=30, headers=headers)
            dl.raise_for_status()
            resp = self._session.post(
                self._url("sendPhoto"),
                data=payload,
                files={"photo": ("photo.jpg", dl.content, dl.headers.get("Content-Type", "image/jpeg"))},
                timeout=30,
            )
            if not resp.ok:
                logger.warning("sendPhoto upload fallback failed: %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:
            logger.warning("sendPhoto download fallback exception: %s", e)
            return False

    def send_photo(self, photo: str, caption: str = "") -> bool:
        """Send a photo. `photo` can be a local file path or an HTTP(S) URL.
        For URLs, falls back to downloading via local proxy if Telegram can't fetch directly.
        Never raises; returns True on success."""
        payload = {"chat_id": self.chat_id}
        if caption:
            payload["caption"] = caption
        debug_bus.emit("telegram_out", {"method": "sendPhoto", "payload": {"photo": photo, "caption": caption}})
        try:
            if photo.startswith("http://") or photo.startswith("https://"):
                resp = self._session.post(
                    self._url("sendPhoto"),
                    json={**payload, "photo": photo},
                    timeout=30,
                )
                if resp.ok:
                    return True
                # Telegram couldn't fetch the URL — download it ourselves and upload
                logger.info("sendPhoto URL failed (%s), trying download+upload fallback", resp.status_code)
                return self._download_and_upload_photo(photo, payload)
            else:
                with open(photo, "rb") as f:
                    resp = self._session.post(
                        self._url("sendPhoto"),
                        data=payload,
                        files={"photo": f},
                        timeout=30,
                    )
                if not resp.ok:
                    logger.warning("sendPhoto failed: %s %s", resp.status_code, resp.text[:200])
                    return False
                return True
        except Exception as e:
            logger.warning("sendPhoto exception: %s", e)
            return False

    @staticmethod
    def _probe_video(path: str) -> dict:
        """Use ffprobe to get width, height, duration from a local video file."""
        import subprocess
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "v:0", path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                import json
                streams = json.loads(result.stdout).get("streams", [])
                if streams:
                    s = streams[0]
                    info = {}
                    w = int(s.get("width", 0))
                    h = int(s.get("height", 0))
                    # Handle rotation (e.g. 90° rotated phone videos)
                    rotation = int(s.get("tags", {}).get("rotate", 0))
                    if rotation in (90, 270):
                        w, h = h, w
                    if w and h:
                        info["width"] = w
                        info["height"] = h
                    dur = s.get("duration")
                    if dur:
                        info["duration"] = int(float(dur))
                    return info
        except Exception as e:
            logger.debug("ffprobe failed for %s: %s", path, e)
        return {}

    def send_video(self, video: str, caption: str = "", upload_timeout: int = 300,
                   reply_to_message_id: int | None = None) -> bool:
        """Send a video. `video` can be a local file path or an HTTP(S) URL.
        upload_timeout: seconds to wait for the upload to complete (default 300s for large files).
        Never raises; returns True on success."""
        payload = {"chat_id": self.chat_id, "supports_streaming": True}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        debug_bus.emit("telegram_out", {"method": "sendVideo", "payload": {"video": video, "caption": caption}})
        try:
            if video.startswith("http://") or video.startswith("https://"):
                resp = self._session.post(
                    self._url("sendVideo"),
                    json={**payload, "video": video},
                    timeout=60,
                )
                if resp.ok:
                    return True
                logger.info("sendVideo URL failed (%s), trying download+upload fallback", resp.status_code)
                # Download to temp file so we can probe metadata
                import tempfile
                try:
                    headers = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}
                    dl = self._session.get(video, timeout=120, headers=headers)
                    dl.raise_for_status()
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                        tmp.write(dl.content)
                        tmp_path = tmp.name
                    probe = self._probe_video(tmp_path)
                    if probe:
                        payload.update(probe)
                    with open(tmp_path, "rb") as f:
                        resp = self._session.post(
                            self._url("sendVideo"),
                            data=payload,
                            files={"video": ("video.mp4", f, "video/mp4")},
                            timeout=upload_timeout,
                        )
                    import os
                    os.unlink(tmp_path)
                    if not resp.ok:
                        logger.warning("sendVideo upload fallback failed: %s %s", resp.status_code, resp.text[:200])
                        return False
                    return True
                except Exception as e:
                    logger.warning("sendVideo download fallback exception: %s", e)
                    return False
            else:
                # Local file upload
                file_size = 0
                try:
                    import os
                    file_size = os.path.getsize(video)
                except OSError:
                    pass
                logger.info("sendVideo local file: %s (%.1f MB)", video, file_size / 1024 / 1024)
                # Probe video metadata so Telegram mobile displays correct aspect ratio
                probe = self._probe_video(video)
                if probe:
                    payload.update(probe)
                    logger.info("sendVideo probe: %s", probe)
                with open(video, "rb") as f:
                    resp = self._session.post(
                        self._url("sendVideo"),
                        data=payload,
                        files={"video": f},
                        timeout=upload_timeout,
                    )
                if not resp.ok:
                    logger.warning("sendVideo failed: %s %s", resp.status_code, resp.text[:200])
                    return False
                return True
        except Exception as e:
            logger.warning("sendVideo exception: %s", e)
            return False

    def download_file(self, file_id: str, save_path: str) -> bool:
        """Download a Telegram file by file_id and save to save_path.
        Never raises; returns True on success."""
        try:
            resp = self._session.post(
                self._url("getFile"),
                json={"file_id": file_id},
                timeout=10,
            )
            if not resp.ok:
                logger.warning("getFile failed: %s", resp.text[:200])
                return False
            file_path = resp.json()["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            dl = self._session.get(download_url, timeout=60)
            dl.raise_for_status()
            import os
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(dl.content)
            return True
        except Exception as e:
            logger.warning("download_file exception: %s", e)
            return False

    def send_message_with_keyboard(self, text: str, reply_markup: dict, parse_mode: str = "") -> int | None:
        """Send a message with an inline keyboard. Returns message_id or None."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = self._session.post(self._url("sendMessage"), json=payload, timeout=10)
            if resp.ok:
                return resp.json().get("result", {}).get("message_id")
            logger.warning("sendMessage (keyboard) failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("sendMessage (keyboard) exception: %s", e)
        return None

    def edit_message_text(self, message_id: int, text: str, parse_mode: str = "",
                          reply_markup: dict = None) -> bool:
        """Edit an existing message's text (and optionally its inline keyboard). Returns True on success."""
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text[:MAX_MESSAGE_LEN],
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            resp = self._session.post(self._url("editMessageText"), json=payload, timeout=10)
            debug_bus.emit("telegram_out", {"method": "editMessageText", "payload": payload, "status": resp.status_code})
            if resp.ok:
                return True
            logger.warning("editMessageText failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("editMessageText exception: %s", e)
        return False

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        """Answer a callback query (dismiss the loading spinner). Returns True on success."""
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            resp = self._session.post(self._url("answerCallbackQuery"), json=payload, timeout=10)
            return resp.ok
        except Exception as e:
            logger.warning("answerCallbackQuery exception: %s", e)
            return False

    def call_api(self, method: str, **kwargs) -> dict:
        """Generic API call. Raises on HTTP error."""
        resp = self._session.post(self._url(method), json=kwargs, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_updates(self, offset: int, timeout: int = 30) -> list:
        """Long-poll for updates. Returns a list of update dicts."""
        try:
            resp = self._session.post(
                self._url("getUpdates"),
                json={
                    "offset": offset,
                    "timeout": timeout,
                    "allowed_updates": ["message", "edited_message", "callback_query", "message_reaction"],
                },
                timeout=(10, timeout + 5),  # connect=10s, read=timeout+5s
            )
            if resp.ok:
                return resp.json().get("result", [])
            logger.warning("getUpdates failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("getUpdates exception: %s", e)
        return []
