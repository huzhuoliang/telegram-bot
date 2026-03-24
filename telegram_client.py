"""Thin wrapper around the Telegram Bot HTTP API."""

import logging
import requests

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

    def send_message(self, text: str, parse_mode: str = "") -> int | None:
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
            try:
                resp = self._session.post(self._url("sendMessage"), json=payload, timeout=10)
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

    def send_video(self, video: str, caption: str = "") -> bool:
        """Send a video. `video` can be a local file path or an HTTP(S) URL.
        Never raises; returns True on success."""
        payload = {"chat_id": self.chat_id}
        if caption:
            payload["caption"] = caption
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
                try:
                    headers = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}
                    dl = self._session.get(video, timeout=60, headers=headers)
                    dl.raise_for_status()
                    resp = self._session.post(
                        self._url("sendVideo"),
                        data=payload,
                        files={"video": ("video.mp4", dl.content, "video/mp4")},
                        timeout=120,
                    )
                    if not resp.ok:
                        logger.warning("sendVideo upload fallback failed: %s", resp.status_code)
                        return False
                    return True
                except Exception as e:
                    logger.warning("sendVideo download fallback exception: %s", e)
                    return False
            else:
                with open(video, "rb") as f:
                    resp = self._session.post(
                        self._url("sendVideo"),
                        data=payload,
                        files={"video": f},
                        timeout=120,
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
        """Send a message with InlineKeyboardMarkup. Returns message_id or None."""
        payload = {"chat_id": self.chat_id, "text": text, "reply_markup": reply_markup}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = self._session.post(self._url("sendMessage"), json=payload, timeout=10)
            if resp.ok:
                return resp.json().get("result", {}).get("message_id")
            logger.warning("sendMessage(keyboard) failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("sendMessage(keyboard) exception: %s", e)
        return None

    def edit_message_keyboard(self, message_id: int, text: str, reply_markup: dict, parse_mode: str = "") -> bool:
        """Edit an existing message's text and keyboard in-place."""
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = self._session.post(self._url("editMessageText"), json=payload, timeout=10)
            if resp.ok:
                return True
            if "not modified" in resp.text:
                return True
            logger.warning("editMessageText failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("editMessageText exception: %s", e)
        return False

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        """Dismiss the loading spinner on an inline button. Must be called within 10s."""
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            resp = self._session.post(self._url("answerCallbackQuery"), json=payload, timeout=10)
            return resp.ok
        except Exception as e:
            logger.warning("answerCallbackQuery exception: %s", e)
            return False

    def send_by_file_id(self, media_type: str, file_id: str, caption: str = "") -> bool:
        """Re-send an archived file by Telegram file_id (no re-upload). media_type: photo/video/document."""
        method_map = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}
        field_map = {"photo": "photo", "video": "video", "document": "document"}
        method = method_map.get(media_type)
        field = field_map.get(media_type)
        if not method:
            return False
        payload = {"chat_id": self.chat_id, field: file_id}
        if caption:
            payload["caption"] = caption
        try:
            resp = self._session.post(self._url(method), json=payload, timeout=30)
            if resp.ok:
                return True
            logger.warning("%s(file_id) failed: %s %s", method, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("%s(file_id) exception: %s", method, e)
        return False

    def get_updates(self, offset: int, timeout: int = 30) -> list:
        """Long-poll for new updates. Returns list of update dicts, [] on error."""
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "message_reaction", "callback_query"],
        }
        try:
            resp = self._session.post(
                self._url("getUpdates"),
                json=payload,
                timeout=(10, timeout + 5),  # connect=10s, read=timeout+5s
            )
            if resp.ok:
                data = resp.json()
                if data.get("ok"):
                    return data.get("result", [])
                logger.warning("getUpdates not ok: %s", data)
            else:
                logger.warning("getUpdates HTTP %s", resp.status_code)
        except requests.exceptions.ReadTimeout:
            pass  # normal long-poll timeout with no messages
        except Exception as e:
            logger.warning("getUpdates exception: %s", e)
        return []
