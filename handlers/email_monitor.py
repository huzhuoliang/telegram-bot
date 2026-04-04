"""Email monitor handler: IMAP monitoring with AI classification and Telegram alerts."""

import datetime
import email as email_lib
import html
import imaplib
import json
import logging
import os
import re
import select
import smtplib
import threading
import time
from email.header import decode_header
from email.mime.text import MIMEText
from pathlib import Path

import debug_bus

logger = logging.getLogger(__name__)

# Max UIDs to keep in state per account (rolling window)
_MAX_STORED_UIDS = 500
# Body preview length for AI classification (chars)
_BODY_PREVIEW_LEN = 2000
# IDLE timeout before re-IDLE (seconds, RFC 2177 recommends < 30 min)
_IDLE_TIMEOUT = 29 * 60


class EmailMonitorHandler:
    """IMAP email monitor with AI classification, summarization, and Telegram alerts."""

    def __init__(
        self,
        credentials_path: str,
        state_path: str,
        telegram_client,
        claude_model: str = "claude-sonnet-4-6",
        claude_max_tokens: int = 200,
        digest_interval_hours: float = 6.0,
        urgent_keywords: list[str] | None = None,
        check_interval: int = 60,
        shutdown_event: threading.Event | None = None,
    ):
        self._credentials_path = Path(credentials_path)
        self._state_path = Path(state_path)
        self._client = telegram_client
        self._claude_model = claude_model
        self._claude_max_tokens = claude_max_tokens
        self._digest_interval_hours = digest_interval_hours
        self._urgent_keywords = urgent_keywords or ["urgent", "emergency", "action required"]
        self._check_interval = check_interval
        self._shutdown_event = shutdown_event or threading.Event()

        self._state_lock = threading.Lock()
        self._digest_lock = threading.Lock()
        self._state: dict = {}
        self._pending_digest: list[dict] = []
        self._account_status: dict[str, str] = {}  # account_id -> "running"/"paused"/"error: ..."
        self._paused = threading.Event()  # set = paused
        self._check_now_events: dict[str, threading.Event] = {}
        self._threads: list[threading.Thread] = []
        self._anthropic_client = None
        self._retry_count: dict[str, int] = {}

        self._load_state()

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self):
        """Start monitoring threads for all configured accounts."""
        accounts = self._load_credentials()
        if not accounts:
            logger.warning("No email accounts configured; email monitor not started")
            return

        for account in accounts:
            aid = account["id"]
            self._check_now_events[aid] = threading.Event()
            t = threading.Thread(
                target=self._monitor_thread,
                args=(account,),
                name=f"email-{aid}",
                daemon=True,
            )
            self._threads.append(t)
            t.start()

        # Digest scheduler thread
        t = threading.Thread(
            target=self._digest_thread,
            name="email-digest",
            daemon=True,
        )
        self._threads.append(t)
        t.start()

        logger.info("Email monitor started for %d account(s)", len(accounts))

    # ── Command dispatch (from /email ...) ───────────────────────

    def handle_command(self, subcommand: str) -> str | None:
        """Handle /email subcommands. Returns reply text or None for async ops."""
        sub = subcommand.strip()
        sub_lower = sub.lower()

        if not sub_lower or sub_lower == "status":
            return self._cmd_status()
        if sub_lower == "digest":
            return self._cmd_digest()
        if sub_lower == "check":
            return self._cmd_check()
        if sub_lower == "pause":
            return self._cmd_pause()
        if sub_lower == "resume":
            return self._cmd_resume()
        if sub_lower == "stats":
            return self._cmd_status()
        if sub_lower.startswith("send "):
            return self._cmd_send(sub[5:].strip())

        return (
            "/email — 查看状态\n"
            "/email digest — 立即发送摘要\n"
            "/email check — 立即检查新邮件\n"
            "/email pause — 暂停监控\n"
            "/email resume — 恢复监控\n"
            "/email send — 发送邮件"
        )

    def _cmd_status(self) -> str:
        accounts = self._load_credentials()
        if not accounts:
            return "未配置邮箱账号。"

        _status_map = {
            "connecting": "连接中",
            "connected": "已连接",
            "idle": "监听中",
            "polling": "轮询中",
            "paused": "已暂停",
            "stopped": "已停止",
            "not started": "未启动",
        }

        lines = []
        for acc in accounts:
            aid = acc["id"]
            raw_status = self._account_status.get(aid, "not started")
            if raw_status.startswith("error:"):
                display = f"异常: {raw_status[6:].strip()}"
            else:
                display = _status_map.get(raw_status, raw_status)
            if self._paused.is_set() and raw_status != "paused":
                display += "（已暂停）"
            lines.append(f"{html.escape(acc.get('username', aid))}  {display}")

        with self._state_lock:
            accounts_state = self._state.get("accounts", {})
            last_digest = self._state.get("last_digest")

        # Per-account stats
        for acc in accounts:
            aid = acc["id"]
            data = accounts_state.get(aid, {})
            total = data.get("total_processed", 0)
            urgent = data.get("total_urgent", 0)
            spam = data.get("total_spam", 0)
            normal = total - urgent - spam
            if total > 0:
                lines.append(f"  累计 {total} 封（普通 {normal} / 紧急 {urgent} / 垃圾 {spam}）")

        with self._digest_lock:
            pending = len(self._pending_digest)

        lines.append("")
        lines.append(f"待发摘要: {pending} 封")
        if last_digest:
            lines.append(f"上次摘要: {last_digest}")
        else:
            lines.append("上次摘要: 无")
        lines.append(f"摘要周期: {self._digest_interval_hours}h")

        return "\n".join(lines)

    def _cmd_digest(self) -> str | None:
        threading.Thread(target=self._send_digest, daemon=True).start()
        return "正在生成摘要..."

    def _cmd_check(self) -> str:
        for ev in self._check_now_events.values():
            ev.set()
        return "正在检查所有账号..."

    def _cmd_pause(self) -> str:
        self._paused.set()
        return "邮件监控已暂停。"

    def _cmd_resume(self) -> str:
        self._paused.clear()
        return "邮件监控已恢复。"

    def _cmd_send(self, raw: str) -> str:
        """Parse and send email. Format: /email send <to> <subject> <body>"""
        # Support two formats:
        #   /email send user@example.com 主题 正文内容...
        #   /email send user@example.com
        #   主题
        #   正文内容（多行）
        parts = raw.split("\n", 2)
        if len(parts) >= 3:
            # Multi-line: first line has recipient, second is subject, rest is body
            first_line = parts[0].strip()
            to_addr = first_line
            subject = parts[1].strip()
            body = parts[2].strip()
        elif len(parts) == 2:
            to_addr = parts[0].strip()
            subject = parts[1].strip()
            body = ""
        else:
            # Single line: split by spaces
            tokens = raw.split(None, 2)
            if len(tokens) < 2:
                return (
                    "用法:\n"
                    "<code>/email send 收件人 主题 正文</code>\n\n"
                    "或多行格式:\n"
                    "<code>/email send 收件人\n主题\n正文（支持多行）</code>"
                )
            to_addr = tokens[0]
            subject = tokens[1]
            body = tokens[2] if len(tokens) > 2 else ""

        if "@" not in to_addr:
            return f"无效的收件人地址: {html.escape(to_addr)}"

        accounts = self._load_credentials()
        if not accounts:
            return "未配置邮箱账号。"

        account = accounts[0]
        try:
            self._send_smtp(account, to_addr, subject, body)
            return f"邮件已发送至 {html.escape(to_addr)}"
        except Exception as e:
            logger.warning("Failed to send email: %s", e)
            return f"发送失败: {html.escape(str(e))}"

    def _send_smtp(self, account: dict, to_addr: str, subject: str, body: str):
        """Send email via SMTP SSL."""
        host = account.get("smtp_host", account["host"].replace("imap.", "smtp."))
        port = account.get("smtp_port", 465)
        username = account["username"]
        password = account["password"]

        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = username
        msg["To"] = to_addr
        msg["Subject"] = subject

        with smtplib.SMTP_SSL(host, port) as server:
            server.login(username, password)
            server.send_message(msg)

        logger.info("Email sent from %s to %s: %s", username, to_addr, subject)
        debug_bus.emit("email_sent", {
            "from": username,
            "to": to_addr,
            "subject": subject,
        })

    # ── IMAP monitoring thread (per account) ─────────────────────

    def _monitor_thread(self, account: dict):
        aid = account["id"]
        logger.info("Email monitor thread started for %s", aid)
        self._account_status[aid] = "connecting"

        while not self._shutdown_event.is_set():
            # Pause check
            while self._paused.is_set() and not self._shutdown_event.is_set():
                self._account_status[aid] = "paused"
                self._shutdown_event.wait(timeout=5)
            if self._shutdown_event.is_set():
                break

            try:
                conn = self._connect_imap(account)
                self._retry_count[aid] = 0
                self._account_status[aid] = "connected"

                use_idle = account.get("idle", True) and self._supports_idle(conn)
                if use_idle:
                    self._account_status[aid] = "idle"
                    self._idle_loop(conn, account)
                else:
                    self._account_status[aid] = "polling"
                    self._poll_loop(conn, account)
            except Exception as e:
                logger.warning("Email monitor %s error: %s", aid, e)
                self._account_status[aid] = f"error: {e}"
                debug_bus.emit("email_error", {"account": aid, "error": str(e)})
                retries = self._retry_count.get(aid, 0)
                backoff = min(30 * (2 ** retries), 300)
                self._retry_count[aid] = retries + 1
                self._shutdown_event.wait(timeout=backoff)

        self._account_status[aid] = "stopped"
        logger.info("Email monitor thread stopped for %s", aid)

    def _connect_imap(self, account: dict) -> imaplib.IMAP4_SSL:
        host = account["host"]
        port = account.get("port", 993)
        conn = imaplib.IMAP4_SSL(host, port)
        conn.socket().settimeout(300)
        conn.login(account["username"], account["password"])
        folder = account.get("folders", ["INBOX"])[0]
        conn.select(folder)
        logger.info("IMAP connected to %s as %s", host, account["username"])
        debug_bus.emit("email_connected", {"account": account["id"], "host": host})
        return conn

    def _supports_idle(self, conn: imaplib.IMAP4_SSL) -> bool:
        typ, data = conn.capability()
        if typ == "OK" and data:
            caps = data[0].decode().upper()
            return "IDLE" in caps
        return False

    # ── IMAP IDLE loop ───────────────────────────────────────────

    def _idle_loop(self, conn: imaplib.IMAP4_SSL, account: dict):
        """IMAP IDLE with SSL-safe pending check.

        SSL sockets may buffer data internally so select() alone is unreliable.
        We use select() on the raw socket AND check ssl.pending() to detect
        server-pushed notifications correctly.
        """
        aid = account["id"]

        # Initial fetch
        self._fetch_new_emails(conn, account)

        while not self._shutdown_event.is_set():
            if self._paused.is_set():
                return

            try:
                # Enter IDLE
                tag = conn._new_tag().decode()
                conn.send(f"{tag} IDLE\r\n".encode())
                resp = conn.readline()
                if not resp.startswith(b"+"):
                    logger.warning("IDLE start failed for %s: %s", aid, resp)
                    return

                check_ev = self._check_now_events.get(aid)
                ssl_sock = conn.socket()          # SSLSocket
                raw_sock = ssl_sock.fileno()      # underlying fd for select()
                start = time.monotonic()
                should_fetch = False

                while not self._shutdown_event.is_set():
                    elapsed = time.monotonic() - start
                    remaining = _IDLE_TIMEOUT - elapsed
                    if remaining <= 0:
                        break

                    wait = min(remaining, 2.0)
                    r, _, _ = select.select([raw_sock], [], [], wait)

                    # Check both: select() result AND SSL internal buffer
                    if r or ssl_sock.pending() > 0:
                        should_fetch = True
                        logger.info("IDLE notification for %s", aid)
                        break
                    if self._paused.is_set():
                        break
                    if check_ev and check_ev.is_set():
                        check_ev.clear()
                        should_fetch = True
                        break

                # Exit IDLE
                conn.send(b"DONE\r\n")
                ssl_sock.settimeout(10)
                try:
                    while True:
                        line = conn.readline()
                        if not line or line.startswith(tag.encode()):
                            break
                finally:
                    ssl_sock.settimeout(300)

                if should_fetch:
                    self._fetch_new_emails(conn, account)

            except (imaplib.IMAP4.error, OSError, ConnectionError) as e:
                logger.warning("IDLE error for %s: %s", aid, e)
                return

    # ── IMAP poll loop ───────────────────────────────────────────

    def _poll_loop(self, conn: imaplib.IMAP4_SSL, account: dict):
        aid = account["id"]

        while not self._shutdown_event.is_set():
            if self._paused.is_set():
                return

            self._fetch_new_emails(conn, account)

            # Wait for next check or manual trigger
            check_ev = self._check_now_events.get(aid)
            waited = 0
            while waited < self._check_interval and not self._shutdown_event.is_set():
                if self._paused.is_set():
                    return
                if check_ev and check_ev.is_set():
                    check_ev.clear()
                    break
                self._shutdown_event.wait(timeout=5)
                waited += 5

            # Keep-alive
            try:
                conn.noop()
            except (imaplib.IMAP4.error, OSError):
                return  # reconnect via outer loop

    # ── Fetch & parse ────────────────────────────────────────────

    def _fetch_new_emails(self, conn: imaplib.IMAP4_SSL, account: dict):
        aid = account["id"]
        with self._state_lock:
            acc_state = self._state.setdefault("accounts", {}).setdefault(aid, {})
            processed = set(acc_state.get("processed_uids", []))
            last_uid = acc_state.get("last_uid", "0")

        try:
            if last_uid != "0":
                status, data = conn.uid("SEARCH", None, f"UID {int(last_uid)+1}:*")
            else:
                since = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
                status, data = conn.uid("SEARCH", None, f"SINCE {since}")

            if status != "OK" or not data[0]:
                return

            uids = data[0].split()
            new_uids = [u for u in uids if u.decode() not in processed]

            if not new_uids:
                return

            # Limit per-cycle to avoid flooding (process newest first)
            max_per_cycle = 20
            if len(new_uids) > max_per_cycle:
                logger.info("Found %d new email(s) for %s, processing latest %d",
                            len(new_uids), aid, max_per_cycle)
                # Mark older ones as processed (skip) to avoid backlog
                skip_uids = new_uids[:-max_per_cycle]
                new_uids = new_uids[-max_per_cycle:]
                with self._state_lock:
                    acc = self._state.setdefault("accounts", {}).setdefault(aid, {})
                    stored = acc.setdefault("processed_uids", [])
                    for u in skip_uids:
                        uid_s = u.decode()
                        if uid_s not in stored:
                            stored.append(uid_s)
                    if len(stored) > _MAX_STORED_UIDS:
                        acc["processed_uids"] = stored[-_MAX_STORED_UIDS:]
            else:
                logger.info("Found %d new email(s) for %s", len(new_uids), aid)

            debug_bus.emit("email_fetch", {"account": aid, "count": len(new_uids)})

            for uid in new_uids:
                uid_str = uid.decode()
                try:
                    status, msg_data = conn.uid("FETCH", uid, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue

                    raw = msg_data[0][1]
                    parsed = self._parse_email(raw)
                    parsed["uid"] = uid_str
                    parsed["account_id"] = aid

                    result = self._classify_and_summarize(parsed)
                    self._store_result(aid, result)

                    if result["classification"] == "urgent":
                        self._send_urgent_alert(result)

                except Exception as e:
                    logger.warning("Error processing email UID %s: %s", uid_str, e)

            self._save_state()

        except (imaplib.IMAP4.error, OSError) as e:
            logger.warning("Fetch error for %s: %s", aid, e)
            raise

    def _parse_email(self, raw_bytes: bytes) -> dict:
        msg = email_lib.message_from_bytes(raw_bytes)

        subject = self._decode_header_value(msg["Subject"])
        sender = self._decode_header_value(msg["From"])
        date_str = msg.get("Date", "")

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(errors="replace")
                    break
                elif ct == "text/html" and not body:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = self._strip_html(payload.decode(errors="replace"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(errors="replace")

        body = body[:_BODY_PREVIEW_LEN].strip()

        has_attachments = False
        if msg.is_multipart():
            has_attachments = any(
                p.get_content_disposition() == "attachment" for p in msg.walk()
            )

        return {
            "subject": subject or "(no subject)",
            "sender": sender or "(unknown)",
            "date": date_str,
            "body_preview": body,
            "has_attachments": has_attachments,
        }

    @staticmethod
    def _decode_header_value(value: str | None) -> str:
        if not value:
            return ""
        parts = decode_header(value)
        decoded = []
        for data, charset in parts:
            if isinstance(data, bytes):
                decoded.append(data.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(data)
        return " ".join(decoded)

    @staticmethod
    def _strip_html(text: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ── AI classification ────────────────────────────────────────

    def _get_api_client(self):
        if self._anthropic_client is None:
            import anthropic
            self._anthropic_client = anthropic.Anthropic()
        return self._anthropic_client

    def _classify_and_summarize(self, email_info: dict) -> dict:
        prompt = (
            "Classify and summarize this email. Respond in JSON only.\n\n"
            f"From: {email_info['sender']}\n"
            f"Subject: {email_info['subject']}\n"
            f"Date: {email_info['date']}\n"
            f"Body:\n{email_info['body_preview']}\n\n"
            'Format: {"classification": "urgent|normal|spam", '
            '"summary": "<1-2 sentence Chinese summary>", '
            '"reason": "<brief reason>"}\n\n'
            "Rules:\n"
            "- urgent: time-sensitive, important financial/legal/health/security matters\n"
            "- spam: marketing, newsletters, promotions, automated notifications\n"
            "- normal: legitimate but not time-critical"
        )

        try:
            client = self._get_api_client()
            response = client.messages.create(
                model=self._claude_model,
                max_tokens=self._claude_max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            # Extract JSON from response (may have markdown wrapping)
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                result = json.loads(m.group())
                email_info["classification"] = result.get("classification", "normal")
                email_info["summary"] = result.get("summary", email_info["subject"])
                email_info["reason"] = result.get("reason", "")
            else:
                raise ValueError("No JSON in response")

        except Exception as e:
            logger.warning("AI classification failed: %s", e)
            email_info["classification"] = "normal"
            email_info["summary"] = email_info["subject"]
            email_info["reason"] = "AI classification unavailable"

        debug_bus.emit("email_classified", {
            "account": email_info.get("account_id"),
            "subject": email_info["subject"],
            "classification": email_info["classification"],
        })

        return email_info

    # ── Store & state ────────────────────────────────────────────

    def _store_result(self, account_id: str, result: dict):
        with self._digest_lock:
            self._pending_digest.append(result)

        with self._state_lock:
            acc = self._state.setdefault("accounts", {}).setdefault(account_id, {})
            uids = acc.setdefault("processed_uids", [])
            uid = result.get("uid", "")
            if uid and uid not in uids:
                uids.append(uid)
            # Rolling window
            if len(uids) > _MAX_STORED_UIDS:
                acc["processed_uids"] = uids[-_MAX_STORED_UIDS:]
            # Update last_uid (keep the max)
            if uid:
                current = acc.get("last_uid", "0")
                if int(uid) > int(current):
                    acc["last_uid"] = uid
            acc["last_check"] = datetime.datetime.now().isoformat(timespec="seconds")
            acc["total_processed"] = acc.get("total_processed", 0) + 1
            if result.get("classification") == "urgent":
                acc["total_urgent"] = acc.get("total_urgent", 0) + 1
            elif result.get("classification") == "spam":
                acc["total_spam"] = acc.get("total_spam", 0) + 1

    def _load_state(self):
        if self._state_path.exists():
            try:
                with open(self._state_path) as f:
                    self._state = json.load(f)
                logger.info("Loaded email state from %s", self._state_path)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load email state, starting fresh: %s", e)
                self._state = {}
        else:
            self._state = {}

    def _save_state(self):
        with self._state_lock:
            data = json.dumps(self._state, indent=2, ensure_ascii=False)
        tmp = self._state_path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                f.write(data)
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.warning("Failed to save email state: %s", e)

    def _load_credentials(self) -> list[dict]:
        if not self._credentials_path.exists():
            logger.warning("Email credentials file not found: %s", self._credentials_path)
            return []
        try:
            with open(self._credentials_path) as f:
                creds = json.load(f)
            return creds.get("accounts", [])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load email credentials: %s", e)
            return []

    # ── Urgent alert ─────────────────────────────────────────────

    def _send_urgent_alert(self, email_info: dict):
        alert = (
            f"<b>[紧急邮件]</b>\n\n"
            f"<b>主题:</b> {html.escape(email_info.get('subject', ''))}\n"
            f"<b>发件人:</b> {html.escape(email_info.get('sender', ''))}\n"
            f"<b>账号:</b> {html.escape(email_info.get('account_id', ''))}\n\n"
            f"{html.escape(email_info.get('summary', ''))}\n\n"
            f"<i>{html.escape(email_info.get('reason', ''))}</i>"
        )
        try:
            self._client.send_message(alert, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to send urgent alert: %s", e)

        debug_bus.emit("email_urgent_alert", {
            "account": email_info.get("account_id"),
            "subject": email_info.get("subject"),
        })

    # ── Digest ───────────────────────────────────────────────────

    def _digest_thread(self):
        interval_secs = self._digest_interval_hours * 3600
        logger.info("Digest thread started (interval: %.1fh)", self._digest_interval_hours)

        while not self._shutdown_event.is_set():
            self._shutdown_event.wait(timeout=interval_secs)
            if self._shutdown_event.is_set():
                break
            self._send_digest()

    def _send_digest(self):
        with self._digest_lock:
            emails = list(self._pending_digest)
            self._pending_digest.clear()

        if not emails:
            logger.info("No emails for digest")
            try:
                self._client.send_message("暂无待发邮件。")
            except Exception:
                pass
            return

        # Build structured data for AI summarization
        non_spam = [e for e in emails if e.get("classification") != "spam"]
        spam_count = len(emails) - len(non_spam)

        email_entries = []
        for e in non_spam:
            entry = (
                f"- 发件人: {e.get('sender', '')}\n"
                f"  主题: {e.get('subject', '')}\n"
                f"  分类: {e.get('classification', 'normal')}\n"
                f"  单封摘要: {e.get('summary', '')}"
            )
            email_entries.append(entry)

        prompt = (
            f"你是邮件助手。以下是用户最近收到的 {len(non_spam)} 封邮件信息"
            f"（另有 {spam_count} 封垃圾邮件已过滤）。\n\n"
            + "\n\n".join(email_entries) + "\n\n"
            "请用中文写一段邮件摘要报告，要求：\n"
            "1. 用自然流畅的语言，不要逐条罗列\n"
            "2. 紧急/重要邮件要突出说明，给出具体内容\n"
            "3. 同类邮件（如多封营销通知）合并概述\n"
            "4. 详略得当：重要的详细说，不重要的一笔带过\n"
            "5. 最后用一句话总结整体情况\n"
            "6. 不要使用 Markdown 格式，用纯文本"
        )

        try:
            client = self._get_api_client()
            response = client.messages.create(
                model=self._claude_model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            digest_text = response.content[0].text.strip()
        except Exception as e:
            logger.warning("AI digest generation failed: %s, falling back", e)
            # Fallback to simple list
            lines = [f"邮件摘要（共 {len(emails)} 封，{spam_count} 封垃圾已过滤）\n"]
            for e in non_spam:
                tag = "[紧急] " if e.get("classification") == "urgent" else ""
                lines.append(f"{tag}{e.get('subject', '')} — {e.get('summary', '')}")
            digest_text = "\n".join(lines)

        for chunk in self._split_message(digest_text, 4096):
            try:
                self._client.send_message(chunk)
            except Exception as e:
                logger.warning("Failed to send digest: %s", e)

        with self._state_lock:
            self._state["last_digest"] = datetime.datetime.now().isoformat(timespec="seconds")
        self._save_state()

        logger.info("Digest sent: %d emails", len(emails))
        debug_bus.emit("email_digest_sent", {"count": len(emails)})

    @staticmethod
    def _split_message(text: str, max_len: int) -> list[str]:
        if len(text) <= max_len:
            return [text]
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            # Find last newline within limit
            idx = text.rfind("\n", 0, max_len)
            if idx <= 0:
                idx = max_len
            chunks.append(text[:idx])
            text = text[idx:].lstrip("\n")
        return chunks
