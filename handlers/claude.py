"""Claude AI handler with CLI and API backends."""

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

import debug_bus
from handlers.common import (
    BASE_DIR,
    _ACTION_RE, _CMD_RE, _LATEX_RE, _LATEX_FENCE_RE, _LATEX_DOC_RE,
    _ensure_pre_language, _protect_file_paths, _run_cmd, _cmd_executable,
    _convert_md_tables, _render_latex, _build_system_prompt,
)

logger = logging.getLogger(__name__)


class ClaudeHandler:
    """Supports two backends, switchable via the 'claude_backend' config key:

    - "cli"  (default): invokes `claude -p <text>` as a subprocess.
              Uses existing Claude Code credentials; no API key needed.
              Each call is stateless (no conversation history).

    - "api": calls Anthropic SDK directly.
              Requires ANTHROPIC_API_KEY env var.
              Maintains a rolling conversation history (claude_history_turns).

    Both backends support action markers ([PHOTO: url], [VIDEO: url]) in
    Claude's response, which are automatically extracted and sent via Telegram.
    """

    def __init__(
        self,
        backend: str = "cli",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
        history_turns: int = 6,
        max_rounds: int = 5,
        cli_timeout: int = 60,
        telegram_client=None,
        allowed_commands: list[str] = None,
    ):
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens
        self.history_turns = history_turns
        self.max_rounds = max_rounds
        self.cli_timeout = cli_timeout
        self._telegram_client = telegram_client
        self._allowed_commands: list[str] = allowed_commands or []
        self._system_prompt = _build_system_prompt(self._allowed_commands, backend)
        self._history: list[dict] = []
        self._lock = threading.Lock()
        self._api_client = None  # lazy-init only if backend == "api"
        self._email_monitor = None  # EmailMonitorHandler reference for tool access
        self._session_input_tokens = 0
        self._session_output_tokens = 0
        self._compress_interactions = False  # subclasses may enable
        self._intermediate_sent = False  # set True when any msg is sent between "处理中..." and final reply

    def set_email_monitor(self, handler):
        """Set EmailMonitorHandler reference for email query tool."""
        self._email_monitor = handler

    def _get_api_client(self):
        if self._api_client is None:
            import anthropic
            self._api_client = anthropic.Anthropic(timeout=300.0)
        return self._api_client

    def _execute_actions(self, response: str) -> str:
        """Extract [PHOTO:] / [VIDEO:] / [LATEX:] markers, execute them, return cleaned text."""
        if not self._telegram_client:
            return response

        response = _ensure_pre_language(response)
        response = _protect_file_paths(response)

        # Handle LATEX markers (both [LATEX:...] and ```latex...``` code fences)
        def _run_latex(match):
            source = match.group(1).strip()
            # If source is just the body without \documentclass, wrap it
            if not source.lstrip().startswith('\\documentclass'):
                source = (
                    "\\documentclass[12pt]{article}\n"
                    "\\usepackage{amsmath,amssymb,geometry}\n"
                    "\\usepackage{xeCJK}\n"
                    "\\geometry{margin=2cm}\n"
                    "\\begin{document}\n"
                    + source +
                    "\n\\end{document}"
                )
            logger.info("Rendering LaTeX (%d chars)", len(source))
            png_pages, err = _render_latex(source)
            if err:
                logger.warning("LaTeX render error: %s", err[:200])
                self._telegram_client.send_message(f"⚠️ LaTeX 渲染失败：{err}", parse_mode="HTML")
            else:
                for p in png_pages:
                    self._telegram_client.send_photo(p)
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            return ""

        response = _LATEX_RE.sub(_run_latex, response)
        response = _LATEX_FENCE_RE.sub(_run_latex, response)
        # Fallback: detect any raw \documentclass...\end{document} in the response
        response = _LATEX_DOC_RE.sub(_run_latex, response)

        def _run(match):
            kind = match.group(1)
            target = match.group(2).strip()
            caption = (match.group(3) or "").strip()
            logger.info("Action from Claude: %s %s", kind, target)
            if kind == "PHOTO":
                ok = self._telegram_client.send_photo(target, caption)
                if not ok:
                    self._telegram_client.send_message(f"⚠️ 图片发送失败：{target[:100]}")
            elif kind == "VIDEO":
                ok = self._telegram_client.send_video(target, caption)
                if not ok:
                    self._telegram_client.send_message(f"⚠️ 视频发送失败：{target[:100]}")
            return ""  # remove marker from text reply

        cleaned = _ACTION_RE.sub(_run, response).strip()
        # If the entire response was action markers, ensure we still send something
        return cleaned or "已发送。"

    def _extract_and_run_cmds(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Extract [CMD:] markers, run whitelisted ones.
        Returns (text_without_cmd_markers, [(cmd, output), ...])."""
        results = []

        def _run(match):
            cmd = match.group(1).strip()
            exe = _cmd_executable(cmd)
            if exe not in self._allowed_commands:
                logger.warning("Blocked CMD (not in whitelist): %s", cmd)
                results.append((cmd, f"(拒绝执行：{exe} 不在白名单中)"))
                return ""
            logger.info("Claude CMD: %s", cmd)
            output = _run_cmd(cmd)
            results.append((cmd, output))
            return ""

        cleaned = _CMD_RE.sub(_run, text).strip()
        return cleaned, results

    def _call_cli(self, text: str) -> str:
        # CLI backend: single-shot call via `claude -p`.
        # Do NOT inject the TOOLS section — Claude Code CLI has its own tool
        # execution system that would intercept [CMD:] markers and show
        # permission prompts instead of outputting the markers as text.
        cli_prompt = _build_system_prompt([], "cli")  # no tools for cli backend
        prompt = f"{cli_prompt}\nUser: {text}"
        for _ in range(3):  # max 3 tool-call rounds
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True, text=True, timeout=self.cli_timeout,
            )
            raw = result.stdout.strip() or result.stderr.strip() or "（无响应）"
            logger.info("Claude raw response: %s", raw[:300])
            _, cmd_results = self._extract_and_run_cmds(raw)
            if not cmd_results:
                break
            # Append tool outputs and re-prompt
            tool_block = "\n".join(f"$ {cmd}\n{out}" for cmd, out in cmd_results)
            prompt += f"\n\nAssistant: {raw}\n\nUser (tool results):\n{tool_block}\n\nPlease give your final answer based on the above."
        return self._execute_actions(_convert_md_tables(raw))

    def _build_tools(self) -> list[dict]:
        """Build the Anthropic tools list based on configuration."""
        tools = [
            {
                "name": "write_latex",
                "description": (
                    "Write (or append) text to a LaTeX source file on the server. "
                    "Use this to build large LaTeX documents in multiple chunks that would "
                    "exceed token limits if passed inline. Call with mode='write' first to "
                    "create/overwrite the file, then mode='append' for each subsequent chunk. "
                    "When done, call render_latex with the same path."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute file path, e.g. /tmp/tgbot_novel.tex",
                        },
                        "content": {
                            "type": "string",
                            "description": "The LaTeX text to write or append.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["write", "append"],
                            "description": "'write' creates/overwrites; 'append' adds to existing file.",
                        },
                    },
                    "required": ["path", "content", "mode"],
                },
            },
            {
                "name": "render_latex",
                "description": (
                    "Compile a LaTeX document and send the rendered image(s) to the user. "
                    "For short documents, pass the full source in 'source'. "
                    "For long documents, first build the file with write_latex, then pass the file path in 'path'."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": (
                                "Inline LaTeX source (\\documentclass … \\end{document}). "
                                "Use XeLaTeX packages. For Chinese add \\usepackage{xeCJK}."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": "Path to an existing .tex file written via write_latex.",
                        },
                    },
                },
            },
        ]
        if self._email_monitor:
            tools.append({
                "name": "query_emails",
                "description": (
                    "Query the user's email inbox via IMAP. Can search all emails (not just recent ones). "
                    "Use this when the user asks about their emails. "
                    "Returns email subjects, senders, dates, and body previews."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["recent", "search"],
                            "description": "'recent' for latest emails, 'search' to search by keyword/sender/date.",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "Search keyword (IMAP subject/body search). For action='search'.",
                        },
                        "sender": {
                            "type": "string",
                            "description": "Filter by sender address or name. For action='search'.",
                        },
                        "days": {
                            "type": "integer",
                            "description": "Only return emails from last N days (default: no limit for search, 7 for recent).",
                        },
                        "count": {
                            "type": "integer",
                            "description": "Max number of emails to return (default 10, max 30).",
                        },
                    },
                    "required": ["action"],
                },
            })
        if self._allowed_commands:
            tools.append({
                "name": "run_command",
                "description": (
                    "Run a whitelisted shell command on the server to fetch real-time information. "
                    f"Allowed executables: {', '.join(self._allowed_commands)}."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute.",
                        }
                    },
                    "required": ["command"],
                },
            })
        return tools

    def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool call and return the result string."""
        if tool_name == "write_latex":
            path = tool_input.get("path", "").strip()
            content = tool_input.get("content", "")
            mode = tool_input.get("mode", "write")
            if not path:
                return "Error: path is required"
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w" if mode == "write" else "a", encoding="utf-8") as f:
                    f.write(content)
                size = os.path.getsize(path)
                logger.info("write_latex %s %s (%d bytes total)", mode, path, size)
                return f"OK: {size} bytes written to {path}"
            except Exception as e:
                return f"Error: {e}"

        if tool_name == "render_latex":
            # Accept either inline source or a file path
            path = tool_input.get("path", "").strip()
            if path:
                try:
                    with open(path, encoding="utf-8") as f:
                        source = f.read()
                except Exception as e:
                    return f"Error reading {path}: {e}"
            else:
                source = tool_input.get("source", "")
            if not source.lstrip().startswith('\\documentclass'):
                source = (
                    "\\documentclass[12pt]{article}\n"
                    "\\usepackage{amsmath,amssymb,geometry}\n"
                    "\\usepackage{xeCJK}\n"
                    "\\geometry{margin=2cm}\n"
                    "\\begin{document}\n"
                    + source +
                    "\n\\end{document}"
                )
            logger.info("Tool: render_latex (%d chars)", len(source))
            png_pages, err = _render_latex(source)
            if err:
                logger.warning("LaTeX render error: %s", err[:200])
                if self._telegram_client:
                    self._telegram_client.send_message(f"⚠️ LaTeX 渲染失败：{err}", parse_mode="HTML")
                return f"Error: {err}"
            if self._telegram_client:
                for p in png_pages:
                    self._telegram_client.send_photo(p)
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            return f"{len(png_pages)} page(s) sent successfully."

        if tool_name == "query_emails":
            if not self._email_monitor:
                return "Error: email monitor not configured"
            return self._email_monitor.query_emails(
                action=tool_input.get("action", "recent"),
                keyword=tool_input.get("keyword", ""),
                sender=tool_input.get("sender", ""),
                days=tool_input.get("days", 0),
                count=min(tool_input.get("count", 10), 30),
            )

        if tool_name == "run_command":
            cmd = tool_input.get("command", "")
            exe = _cmd_executable(cmd)
            if exe not in self._allowed_commands:
                return f"Error: '{exe}' is not in the allowed commands list."
            logger.info("Tool: run_command: %s", cmd)
            return _run_cmd(cmd)

        return f"Error: unknown tool '{tool_name}'"

    @staticmethod
    def _is_text_user_message(msg: dict) -> bool:
        """Return True if msg is a user message with text (not tool_results)."""
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") != "tool_result"
                for b in content
            )
        return True

    @staticmethod
    def _block_has_tool_use(content) -> bool:
        if not isinstance(content, list):
            return False
        for b in content:
            btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if btype == "tool_use":
                return True
        return False

    def _sanitize_history(self):
        """Remove any trailing assistant(tool_use) without a following tool_result.
        This can happen if a previous call crashed mid-round."""
        while (
            len(self._history) >= 1
            and self._history[-1].get("role") == "assistant"
            and self._block_has_tool_use(self._history[-1].get("content", []))
        ):
            logger.warning("Removing orphaned tool_use from history tail")
            self._history.pop()

    def _compress_last_interaction(self):
        """Collapse tool-call intermediates from the last completed interaction.
        Replaces the chain of assistant(tool_use)/user(tool_result) messages with
        a single assistant text message, saving context tokens across turns."""
        if len(self._history) < 2:
            return
        last = self._history[-1]
        if last.get("role") != "assistant":
            return
        # Extract plain text from the final assistant content block
        content = last.get("content", "")
        if isinstance(content, str):
            final_text = content
        elif isinstance(content, list):
            parts = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        parts.append(b.get("text", ""))
                else:
                    if getattr(b, "type", None) == "text":
                        parts.append(getattr(b, "text", ""))
            final_text = "\n".join(parts).strip()
        else:
            return
        if not final_text:
            return
        # Walk back to find the originating user text message
        i = len(self._history) - 2
        while i >= 0 and not self._is_text_user_message(self._history[i]):
            i -= 1
        if i < 0:
            return
        removed = len(self._history) - i - 2  # intermediate messages dropped
        self._history[i:] = [
            self._history[i],
            {"role": "assistant", "content": final_text},
        ]
        if removed > 0:
            logger.debug("Compressed history: removed %d intermediate tool-call messages", removed)

    _CTX_WINDOW = 200_000  # claude-sonnet-4-6 context window

    def _append_usage(self, text: str, input_tokens: int, output_tokens: int) -> str:
        ctx_pct = input_tokens / self._CTX_WINDOW * 100
        stats = (
            f"\n<code>━━━\n"
            f"📊 in {input_tokens:,} · out {output_tokens:,} · ctx {ctx_pct:.1f}%\n"
            f"💰 session in {self._session_input_tokens:,} / out {self._session_output_tokens:,}</code>"
        )
        return text + stats

    def _call_api(self, text: str, image_data: bytes = None) -> str:
        # history is already locked by caller.
        # Save state so we can roll back on any error.
        import base64 as _b64
        self._sanitize_history()
        saved_history = list(self._history)
        try:
            if image_data:
                content = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": _b64.b64encode(image_data).decode(),
                        },
                    },
                    {"type": "text", "text": text},
                ]
            else:
                content = text
            self._history.append({"role": "user", "content": content})
            max_msgs = self.history_turns * 2
            if len(self._history) > max_msgs:
                trimmed = self._history[-max_msgs:]
                # Advance past any leading tool_result / assistant messages so
                # we never send an orphaned tool_use block to the API.
                start = 0
                while start < len(trimmed) and not self._is_text_user_message(trimmed[start]):
                    start += 1
                self._history = trimmed[start:]

            client = self._get_api_client()
            tools = self._build_tools()

            last_input_tokens = 0
            total_output_tokens = 0

            for _round in range(self.max_rounds):
                debug_bus.emit("api_request", {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": self._system_prompt,
                    "messages": self._history,
                    "tools": [t.get("name", "") for t in tools],
                    "round": _round,
                })
                response = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self._system_prompt,
                    messages=self._history,
                    tools=tools,
                )
                last_input_tokens = response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens

                # Collect text and tool_use blocks
                text_parts = []
                tool_calls = []
                for block in response.content:
                    if block.type == "text":
                        text_parts.append(block.text)
                    elif block.type == "tool_use":
                        tool_calls.append(block)

                if response.stop_reason != "tool_use" or not tool_calls:
                    # Final text response
                    raw = "\n".join(text_parts).strip()
                    debug_bus.emit("api_response", {
                        "stop_reason": response.stop_reason,
                        "text": raw,
                        "usage": {"input_tokens": last_input_tokens, "output_tokens": response.usage.output_tokens},
                        "tool_calls": [],
                        "round": _round,
                    })
                    logger.info("Claude raw response (stop=%s): %s", response.stop_reason, raw[:500])
                    cleaned = self._execute_actions(_convert_md_tables(raw))
                    self._history.append({"role": "assistant", "content": response.content})
                    self._session_input_tokens += last_input_tokens
                    self._session_output_tokens += total_output_tokens
                    if self._compress_interactions:
                        self._compress_last_interaction()
                    return self._append_usage(cleaned or "已完成。", last_input_tokens, total_output_tokens)

                # Execute tool calls and feed results back
                debug_bus.emit("api_response", {
                    "stop_reason": response.stop_reason,
                    "text": "\n".join(text_parts).strip(),
                    "usage": {"input_tokens": last_input_tokens, "output_tokens": response.usage.output_tokens},
                    "tool_calls": [{"name": tc.name, "input": tc.input} for tc in tool_calls],
                    "round": _round,
                })
                self._history.append({"role": "assistant", "content": response.content})
                tool_results = []
                for tc in tool_calls:
                    result = self._handle_tool_call(tc.name, tc.input)
                    debug_bus.emit("tool_call", {"name": tc.name, "input": tc.input, "result": result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    })
                self._history.append({"role": "user", "content": tool_results})

            # Exhausted rounds — response.content (tool_use) was already appended
            # inside the loop; do NOT append again to avoid orphaned tool_use blocks.
            raw = "\n".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            cleaned = self._execute_actions(_convert_md_tables(raw))
            self._session_input_tokens += last_input_tokens
            self._session_output_tokens += total_output_tokens
            if self._compress_interactions:
                self._compress_last_interaction()
            return self._append_usage(cleaned or "已完成。", last_input_tokens, total_output_tokens)

        except Exception:
            self._history = saved_history  # roll back all history changes
            raise

    def handle(self, text: str) -> str:
        if not text:
            return "用法：?<问题>"
        logger.info("Claude [%s]: %s", self.backend, text[:80])
        processing_msg_id = None
        self._intermediate_sent = False
        if self._telegram_client:
            processing_msg_id = self._telegram_client.send_message("⏳ 处理中...")
        with self._lock:
            try:
                if self.backend == "api":
                    reply = self._call_api(text)
                else:
                    reply = self._call_cli(text)
            except subprocess.TimeoutExpired:
                reply = f"Claude 响应超时（{self.cli_timeout} 秒）"
            except FileNotFoundError:
                reply = "错误：PATH 中未找到 claude 命令"
            except Exception as e:
                # _call_api rolls back history on error; nothing to do here.
                logger.warning("Claude error: %s", e)
                reply = f"Claude 错误：{e}"
        if processing_msg_id and self._telegram_client:
            # If intermediate messages were sent (e.g. tool confirmations), "处理中..." is
            # no longer at the bottom — delete it and send a fresh message so the reply
            # appears after all intermediate messages.
            if self._intermediate_sent:
                self._telegram_client.delete_message(processing_msg_id)
                self._telegram_client.send_message(reply, parse_mode="HTML")
            elif not self._telegram_client.edit_message_text(processing_msg_id, reply, "HTML"):
                self._telegram_client.delete_message(processing_msg_id)
                self._telegram_client.send_message(reply, parse_mode="HTML")
            return None
        return reply

    def handle_with_image(self, text: str, file_id: str) -> str:
        """Download a Telegram photo and send it to Claude for recognition (api backend only)."""
        if self.backend != "api":
            return "⚠️ 识图仅支持 api backend。"
        import tempfile
        logger.info("Claude image [%s]: %s", self.backend, text[:80])
        processing_msg_id = None
        self._intermediate_sent = False
        if self._telegram_client:
            processing_msg_id = self._telegram_client.send_message("⏳ 处理中...")
        tmp_path = None
        image_data = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name
            ok = self._telegram_client.download_file(file_id, tmp_path)
            if ok:
                with open(tmp_path, "rb") as f:
                    image_data = f.read()
        except Exception as e:
            logger.warning("Image download failed: %s", e)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        with self._lock:
            try:
                reply = self._call_api(text, image_data=image_data)
            except Exception as e:
                logger.warning("Claude image error: %s", e)
                reply = f"Claude 错误：{e}"
        if processing_msg_id and self._telegram_client:
            if self._intermediate_sent:
                self._telegram_client.delete_message(processing_msg_id)
                self._telegram_client.send_message(reply, parse_mode="HTML")
            elif not self._telegram_client.edit_message_text(processing_msg_id, reply, "HTML"):
                self._telegram_client.delete_message(processing_msg_id)
                self._telegram_client.send_message(reply, parse_mode="HTML")
            return None
        return reply

    def help(self) -> str:
        with self._lock:
            backend = self.backend
            allowed = list(self._allowed_commands)
        help_file = BASE_DIR / "help.txt"
        static = help_file.read_text(encoding="utf-8").rstrip()
        lines = [static, f"\n<b>当前 Backend：</b><code>{backend}</code>"]
        if backend == "api":
            lines.append("  ✅ 支持多轮对话历史")
            if allowed:
                lines.append(f"  ✅ 可用工具命令：{', '.join(f'<code>{c}</code>' for c in allowed)}")
                lines.append("     直接提问，Claude 会按需自动调用")
        else:
            lines.append("  ℹ️ 无对话历史（每次独立）")
            if allowed:
                lines.append(f"  ℹ️ 工具命令（{', '.join(allowed)}）需切换到 api backend 后生效")
        return self._send_html("\n".join(lines))

    def _send_html(self, text: str):
        """Send HTML-formatted text via telegram client; return text if no client."""
        if self._telegram_client:
            self._telegram_client.send_message(text, parse_mode="HTML")
            return None
        return text

    def clear_history(self) -> str:
        with self._lock:
            self._history.clear()
        return self._send_html("对话历史已清除。")

    def _update_config_backend(self, backend: str, config_path: str) -> bool:
        """Write claude_backend value to config.json. Returns True on success."""
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            cfg["claude_backend"] = backend
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
                f.write("\n")
            return True
        except Exception as e:
            logger.warning("Failed to update config: %s", e)
            return False

    def configure_api_backend(self, api_key: str, config_path: str = None) -> str:
        """Validate ANTHROPIC_API_KEY, switch to api backend, persist both to disk."""
        api_key = api_key.strip()

        # Validate key before saving
        try:
            import anthropic
            test_client = anthropic.Anthropic(api_key=api_key)
            test_client.messages.create(
                model=self.model,
                max_tokens=8,
                messages=[{"role": "user", "content": "hi"}],
            )
        except Exception as e:
            return self._send_html(f"❌ API key 验证失败：<code>{e}</code>")

        os.environ["ANTHROPIC_API_KEY"] = api_key

        # Persist key to api_key.txt in project root
        key_file = BASE_DIR / "api_key.txt"
        key_file.write_text(api_key + "\n")
        key_file.chmod(0o600)

        # Switch backend and reset client + history
        with self._lock:
            self.backend = "api"
            self._system_prompt = _build_system_prompt(self._allowed_commands, "api")
            self._api_client = None
            self._history.clear()

        if config_path and not self._update_config_backend("api", config_path):
            return self._send_html("⚠️ API key 验证通过并已生效，但写入 config.json 失败，重启后需重新设置。")

        return self._send_html("✅ API key 验证通过，已切换到 <code>api</code> backend，对话历史已清除。")

    def status(self) -> str:
        with self._lock:
            backend = self.backend
            history_len = len(self._history) // 2  # turns
        if backend == "api":
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            key_display = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "(未设置)"
            text = (
                f"Backend: <b>api</b>（Anthropic SDK）\n"
                f"Model: <code>{self.model}</code>\n"
                f"API key: <code>{key_display}</code>\n"
                f"对话历史：{history_len} 轮（最多 {self.history_turns} 轮）"
            )
        else:
            text = (
                f"Backend: <b>cli</b>（<code>claude -p</code>）\n"
                f"对话历史：无（cli backend 无状态）"
            )
        return self._send_html(text)

    def context_stats(self) -> str:
        """Return a breakdown of context window usage (api backend only)."""
        if self.backend != "api":
            return self._send_html("⚠️ /ctx 仅支持 api backend。")
        with self._lock:
            history_snapshot = list(self._history)
            system_prompt = self._system_prompt
        try:
            client = self._get_api_client()
            tools = self._build_tools()
            dummy = [{"role": "user", "content": "x"}]
            CTX = self._CTX_WINDOW

            r_base   = client.messages.count_tokens(model=self.model, messages=dummy)
            r_system = client.messages.count_tokens(model=self.model, system=system_prompt, messages=dummy)
            r_tools  = client.messages.count_tokens(model=self.model, messages=dummy, tools=tools)
            r_full   = client.messages.count_tokens(
                model=self.model, system=system_prompt,
                messages=history_snapshot if history_snapshot else dummy,
                tools=tools,
            )

            base_n    = r_base.input_tokens
            system_n  = r_system.input_tokens - base_n
            tools_n   = r_tools.input_tokens  - base_n
            total_n   = r_full.input_tokens
            history_n = total_n - system_n - tools_n - base_n
            free_n    = CTX - total_n

            def pct(n): return n / CTX * 100

            bar_filled = int(total_n / CTX * 20)
            bar = "█" * bar_filled + "░" * (20 - bar_filled)

            def row(label, n, extra=""):
                # label: 4 CJK chars (8 display cols), number right-aligned 7 chars, pct 5 chars
                return f"{label}  {n:>7,}  {pct(n):>4.1f}%  {extra}".rstrip()

            table = "\n".join([
                f"[{bar}]",
                f"{total_n:,} / {CTX:,} ({pct(total_n):.1f}%)",
                "",
                row("系统提示", system_n),
                row("工具定义", tools_n),
                row("对话历史", history_n, f"{len(history_snapshot)//2}轮"),
                row("基础开销", base_n),
                "─" * 32,
                row("剩余空间", free_n),
            ])
            header = f"上下文用量 · {self.model}"
            return self._send_html(f"<b>{header}</b>\n<pre>{table}</pre>")
        except Exception as e:
            return self._send_html(f"⚠️ 计算失败：{e}")

    def configure_cli_backend(self, config_path: str = None) -> str:
        """Switch back to cli backend and persist the change."""
        with self._lock:
            self.backend = "cli"
            self._system_prompt = _build_system_prompt(self._allowed_commands, "cli")
            self._history.clear()

        # Remove api_key.txt so it doesn't auto-load on restart
        key_file = BASE_DIR / "api_key.txt"
        if key_file.exists():
            key_file.unlink()

        if config_path and not self._update_config_backend("cli", config_path):
            return self._send_html("⚠️ 已切换到 <code>cli</code> backend，但写入 config.json 失败，重启后需重新设置。")

        return self._send_html("✅ 已切换到 <code>cli</code> backend（<code>claude -p</code>），对话历史已清除。")
