"""Bilibili video archive — persistent record of downloaded videos.

Shared between fav and UP monitors. Maps BV ID to final archive path
(typically NAS path) with source metadata. Used to skip re-download of
already-archived videos and to serve `/<handler> redo <BV>` commands.
"""

import json
import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class BilibiliArchive:
    """Thread-safe archive of BV -> {path, title, source_type, source_id, source_name,
    archived_at, on_nas}. Persists to a single JSON file shared between handlers."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.warning("Failed to load archive: %s", e)
                self._data = {}

    def _save(self):
        """Atomic write. Caller must hold _lock."""
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(self._path)
        except Exception as e:
            logger.warning("Failed to save archive: %s", e)
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

    def has(self, bvid: str) -> bool:
        with self._lock:
            return bvid in self._data

    def get(self, bvid: str) -> dict | None:
        with self._lock:
            entry = self._data.get(bvid)
            return dict(entry) if entry else None

    def add(self, bvid: str, entry: dict):
        """Add or overwrite an entry. Required fields:
        path, title, source_type ("up"|"fav"|"unknown"), source_id, source_name, on_nas.
        Optional fields: owner_mid, owner_name, staff (list), page_type.
        Any other extra fields in `entry` are also persisted verbatim."""
        now = datetime.now(timezone.utc).isoformat()
        base = {
            "path": entry.get("path", ""),
            "title": entry.get("title", ""),
            "source_type": entry.get("source_type", ""),
            "source_id": entry.get("source_id", ""),
            "source_name": entry.get("source_name", ""),
            "archived_at": entry.get("archived_at", now),
            "on_nas": bool(entry.get("on_nas", False)),
        }
        # Preserve any additional fields the caller provided
        for k, v in entry.items():
            if k not in base:
                base[k] = v
        with self._lock:
            self._data[bvid] = base
            self._save()

    def remove(self, bvid: str) -> bool:
        with self._lock:
            if bvid in self._data:
                del self._data[bvid]
                self._save()
                return True
            return False

    def count(self) -> int:
        with self._lock:
            return len(self._data)


def verify_nas_file(nas_host: str, remote_path: str, timeout: int = 10) -> bool:
    """Verify a file exists on NAS via SSH. Uses shell `test -f`.

    Relies on the caller's ~/.ssh/config (ControlMaster recommended for perf).
    Returns False on any error (timeout, network, missing file).
    """
    if not nas_host or not remote_path:
        return False
    # Quote the path for remote shell. Use single quotes and escape any single quotes in path.
    quoted = "'" + remote_path.replace("'", "'\\''") + "'"
    try:
        result = subprocess.run(
            ["ssh", nas_host, f"test -f {quoted}"],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning("NAS verify failed for %s: %s", remote_path, e)
        return False
