"""Shared installer helpers for dedicated Light RIP installers.

Centralizes two concerns that every runtime-specific installer
(Claude, Codex, ...) shares:

  * Pre-write backup with a date-stamped suffix
    ``<path>.bak-YYYY-MM-DD`` (UTC date). This matches the convention
    documented in ``install_general_agent_hook.py`` and gives users a
    one-command manual restore path.

  * Crash-safe writes: text is staged in a temp file in the target
    directory, fsynced, then atomically renamed over the destination
    via ``os.replace``. A crash mid-write leaves the previous config
    intact instead of producing a half-written ``settings.json`` or
    ``config.toml``.

Collision policy
----------------
A backup is considered a collision when a file already exists at the
computed ``<path>.bak-YYYY-MM-DD`` location. By default we ABORT
(``BackupCollisionError``) rather than overwrite; callers must
explicitly pass ``force=True`` (wired to the ``--force-backup`` CLI
flag) to overwrite a pre-existing same-day backup. This protects
against the common case of "I ran install twice today and the second
run silently clobbered the only good backup".

Public API
----------
  * ``BackupCollisionError(Exception)``
  * ``backup_file(path, *, force=False) -> Path | None``
      Returns the backup path on success, or ``None`` if the source
      did not exist (first install).
  * ``atomic_write_text(path, text, *, encoding="utf-8") -> None``
  * ``atomic_write_json(path, payload, *, encoding="utf-8") -> None``
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class BackupCollisionError(Exception):
    """A same-day backup already exists at the target location."""

    def __init__(self, backup_path: Path | str) -> None:
        self.backup_path = str(backup_path)
        super().__init__(
            f"backup already exists at {self.backup_path}; "
            f"pass --force-backup to overwrite, or rename the existing backup"
        )


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def backup_file(path: Path, *, force: bool = False) -> Path | None:
    """Copy ``path`` to ``<path>.bak-YYYY-MM-DD`` (UTC). Returns the
    backup path, or ``None`` if ``path`` does not exist (first run).
    Raises ``BackupCollisionError`` if the backup file already exists
    and ``force`` is not set."""
    if not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = path.with_name(path.name + ".bak-" + _today_utc())
    if backup_path.exists() and not force:
        raise BackupCollisionError(backup_path)
    shutil.copy2(path, backup_path)
    return backup_path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically: stage in a sibling temp
    file, fsync, then ``os.replace`` over the destination. The
    destination's parent directory is created if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        try:
            os.write(fd, text.encode(encoding))
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        # Failed staging: best-effort cleanup of the temp file.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: dict, *, encoding: str = "utf-8") -> None:
    """Serialize ``payload`` as JSON and write atomically."""
    atomic_write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding=encoding,
    )