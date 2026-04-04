"""Privileged Claude handler with unrestricted system access."""

import json
import logging
import os
import threading
from pathlib import Path

from handlers.claude import ClaudeHandler
from handlers.common import (
    _SYSTEM_PROMPT_PRIVILEGED, _build_system_prompt, _run_privileged_cmd,
)

logger = logging.getLogger(__name__)


class PrivilegedClaudeHandler(ClaudeHandler):
    """Unrestricted Claude AI with full shell, file-read, and file-write access.

    Triggered by the '$' prefix in router.py. Always uses the 'api' backend.
    Maintains a separate conversation history from the regular ClaudeHandler.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        history_turns: int = 6,
        max_rounds: int = 20,
        shell_timeout: int = 60,
        telegram_client=None,
        shell_whitelist: list[str] | None = None,
        config_path: str | None = None,
    ):
        super().__init__(
            backend="api",
            model=model,
            max_tokens=max_tokens,
            history_turns=history_turns,
            max_rounds=max_rounds,
            cli_timeout=120,
            telegram_client=telegram_client,
            allowed_commands=[],
        )
        self._shell_timeout = shell_timeout
        self._system_prompt = _SYSTEM_PROMPT_PRIVILEGED
        self._shell_whitelist: list[str] = list(shell_whitelist or [])
        self._config_path = config_path
        # Pending confirmation state (protected by _pending_lock)
        self._pending_lock = threading.Lock()
        self._pending_event: threading.Event | None = None
        self._pending_result: str | None = None   # "approve" | "whitelist" | "reject"
        self._pending_msg_id: int | None = None
        self._pending_cmd: str | None = None
        # Busy flag: prevents concurrent $-command execution (protected by _lock)
        self._busy = False
        # Auto-approve flag: set for the duration of a $$ request (background thread only)
        self._auto_approve = False
        self._compress_interactions = True  # strip tool-call intermediates after each turn

    # ------------------------------------------------------------------
    # Whitelist helpers
    # ------------------------------------------------------------------

    def _is_whitelisted(self, cmd: str) -> bool:
        for pattern in self._shell_whitelist:
            if pattern.endswith("*"):
                if cmd.startswith(pattern[:-1]):
                    return True
            elif cmd == pattern:
                return True
        return False

    def _save_whitelist(self):
        if not self._config_path:
            return
        try:
            with open(self._config_path) as f:
                cfg = json.load(f)
            cfg["privileged_shell_whitelist"] = self._shell_whitelist
            with open(self._config_path, "w") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("Failed to save whitelist to config: %s", e)

    def handle_whitelist_cmd(self, args: str) -> str:
        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else ""

        if not sub or sub == "list":
            if not self._shell_whitelist:
                return self._send_html("白名单为空。")
            lines = ["<b>Shell 命令白名单：</b>"]
            for i, p in enumerate(self._shell_whitelist, 1):
                tag = "（前缀匹配）" if p.endswith("*") else "（精确匹配）"
                safe = p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"  {i}. <code>{safe}</code> {tag}")
            return self._send_html("\n".join(lines))

        if sub == "add":
            pattern = parts[1].strip() if len(parts) > 1 else ""
            if not pattern:
                return self._send_html("用法：<code>$whitelist add &lt;命令或前缀*&gt;</code>")
            if pattern in self._shell_whitelist:
                safe = pattern.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                return self._send_html(f"<code>{safe}</code> 已在白名单中。")
            self._shell_whitelist.append(pattern)
            self._save_whitelist()
            safe = pattern.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            tag = "（前缀匹配）" if pattern.endswith("*") else "（精确匹配）"
            return self._send_html(f"✅ 已添加到白名单：<code>{safe}</code> {tag}")

        if sub == "remove":
            idx_str = parts[1].strip() if len(parts) > 1 else ""
            try:
                idx = int(idx_str)
            except ValueError:
                return self._send_html("用法：<code>$whitelist remove &lt;序号&gt;</code>")
            if idx < 1 or idx > len(self._shell_whitelist):
                return self._send_html(f"序号超出范围，当前白名单共 {len(self._shell_whitelist)} 条。")
            removed = self._shell_whitelist.pop(idx - 1)
            self._save_whitelist()
            safe = removed.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return self._send_html(f"✅ 已从白名单移除：<code>{safe}</code>")

        return self._send_html(
            "用法：\n"
            "<code>$whitelist list</code> — 查看白名单\n"
            "<code>$whitelist add &lt;命令或前缀*&gt;</code> — 添加\n"
            "<code>$whitelist remove &lt;序号&gt;</code> — 删除"
        )

    # ------------------------------------------------------------------
    # Pending confirmation (called from router's reaction handler)
    # ------------------------------------------------------------------

    def resolve_pending(self, result: str) -> bool:
        """Signal the pending confirmation. result: 'approve' | 'whitelist' | 'reject'."""
        with self._pending_lock:
            if self._pending_event is None:
                return False
            self._pending_result = result
            self._pending_event.set()
        return True

    def resolve_pending_callback(self, cq_id: str, msg_id: int | None, result: str):
        """Called from router's callback_query handler. Answers the callback, edits
        the confirmation message to show the decision, then unblocks the waiting thread."""
        if self._telegram_client:
            self._telegram_client.answer_callback_query(cq_id)

        if self._telegram_client and msg_id:
            with self._pending_lock:
                cmd = self._pending_cmd or ""
            safe = cmd.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            labels = {
                "approve":   "✅ 已允许（一次）",
                "whitelist": "✅ 已允许并加入白名单",
                "reject":    "❌ 已拒绝",
            }
            label = labels.get(result, result)
            self._telegram_client.edit_message_text(
                msg_id,
                f"🔐 <b>请求执行命令：</b>\n\n<pre>{safe}</pre>\n\n{label}",
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )

        self.resolve_pending(result)

    def _request_confirmation(self, cmd: str) -> tuple[bool, bool]:
        """Send a confirmation message with inline buttons and block until clicked or timeout.
        Returns (approved, add_to_whitelist)."""
        if self._auto_approve:
            if self._telegram_client:
                safe = cmd.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                self._telegram_client.send_message(
                    f"⚡ 自动执行：\n\n<pre>{safe}</pre>", parse_mode="HTML"
                )
                self._intermediate_sent = True
            logger.info("Privileged CMD (auto-approve): %s", cmd)
            return True, False

        TIMEOUT = 60
        event = threading.Event()
        with self._pending_lock:
            self._pending_event = event
            self._pending_result = None
            self._pending_msg_id = None
            self._pending_cmd = cmd

        if self._telegram_client:
            safe = cmd.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            msg = (
                f"🔐 <b>请求执行命令：</b>\n\n"
                f"<pre>{safe}</pre>\n\n"
                f"<i>（{TIMEOUT} 秒后自动拒绝）</i>"
            )
            markup = {"inline_keyboard": [[
                {"text": "✅ 允许一次",    "callback_data": "priv:approve"},
                {"text": "📌 加入白名单", "callback_data": "priv:whitelist"},
                {"text": "❌ 拒绝",       "callback_data": "priv:reject"},
            ]]}
            msg_id = self._telegram_client.send_message_with_keyboard(msg, markup, parse_mode="HTML")
            self._intermediate_sent = True
            with self._pending_lock:
                self._pending_msg_id = msg_id

        triggered = event.wait(timeout=TIMEOUT)

        with self._pending_lock:
            result = self._pending_result
            self._pending_event = None
            self._pending_msg_id = None
            self._pending_cmd = None

        approved = triggered and result in ("approve", "whitelist")
        # On timeout, send a separate message (button message was already edited by resolve_pending_callback;
        # on timeout nobody clicked so we need to notify manually)
        if not triggered and self._telegram_client:
            safe = cmd.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            self._telegram_client.send_message(
                f"⏱ 已超时自动拒绝：<code>{safe}</code>", parse_mode="HTML"
            )
        return approved, result == "whitelist"

    def _build_tools(self) -> list[dict]:
        return [
            {
                "name": "run_shell_command",
                "description": (
                    "Run any shell command on the server, including sudo commands. "
                    "Returns exit code + combined stdout/stderr."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The shell command to execute."}
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "read_file",
                "description": "Read the full contents of any file by absolute path.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path to read."}
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "write_file",
                "description": (
                    "Write (overwrite) a file at the given absolute path with new content. "
                    "Creates parent directories if needed."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute file path to write."},
                        "content": {"type": "string", "description": "Full new content of the file."},
                    },
                    "required": ["path", "content"],
                },
            },
        ]

    def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "run_shell_command":
            cmd = tool_input.get("command", "").strip()
            if not cmd:
                return "Error: command is empty"
            if self._is_whitelisted(cmd):
                logger.info("Privileged CMD (whitelisted): %s", cmd)
                return _run_privileged_cmd(cmd, timeout=self._shell_timeout)
            approved, add_to_whitelist = self._request_confirmation(cmd)
            if not approved:
                return "REJECTED: User rejected this command."
            if add_to_whitelist:
                if cmd not in self._shell_whitelist:
                    self._shell_whitelist.append(cmd)
                    self._save_whitelist()
            logger.info("Privileged CMD (approved): %s", cmd)
            return _run_privileged_cmd(cmd, timeout=self._shell_timeout)

        if tool_name == "read_file":
            path = tool_input.get("path", "").strip()
            logger.info("Privileged read_file: %s", path)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if len(content) > 8000:
                    content = content[:8000] + "\n…(truncated at 8000 chars)"
                return content
            except Exception as e:
                return f"Error reading {path}: {e}"

        if tool_name == "write_file":
            path = tool_input.get("path", "").strip()
            content = tool_input.get("content", "")
            logger.info("Privileged write_file: %s (%d chars)", path, len(content))
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"OK: wrote {os.path.getsize(path)} bytes to {path}"
            except Exception as e:
                return f"Error writing {path}: {e}"

        return super()._handle_tool_call(tool_name, tool_input)

    def handle(self, text: str, auto_approve: bool = False) -> str | None:
        if not text:
            return self._send_html("用法：<code>$&lt;指令&gt;</code>")
        with self._lock:
            if self._busy:
                return self._send_html("⚠️ 特权 AI 正在处理另一个请求，请稍后再试。")
            self._busy = True

        def _run():
            self._auto_approve = auto_approve
            try:
                super(PrivilegedClaudeHandler, self).handle(text)
            finally:
                self._auto_approve = False
                with self._lock:
                    self._busy = False

        threading.Thread(target=_run, daemon=True, name="privileged-ai").start()
        return None

    def clear_history(self) -> str:
        with self._lock:
            self._history.clear()
        return self._send_html("特权对话历史已清除。")
