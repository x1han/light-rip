"""Shared installer helpers for dedicated Light RIP installers.

Centralizes three concerns every runtime-specific installer (Claude,
Codex, ...) shares:

  * Pre-write backup with a date-stamped suffix
    ``<path>.bak-YYYY-MM-DD`` (UTC date). This matches the convention
    documented in ``install_general_agent_hook.py`` and gives users a
    one-command manual restore path.

  * Crash-safe writes: text is staged in a temp file in the target
    directory, fsynced, then atomically renamed over the destination
    via ``os.replace``. A crash mid-write leaves the previous config
    intact instead of producing a half-written ``settings.json`` or
    ``config.toml``.

  * Safe config load + dedup upsert: ``load_json_safe`` rejects
    non-object roots and returns a typed error key instead of
    raising; ``upsert_light_rip`` removes ALL existing entries in
    the same namespace (not just the first) before appending.

Collision policy
----------------
A backup is considered a collision when a file already exists at the
computed ``<path>.bak-YYYY-MM-DD`` location. By default we ABORT
(``BackupCollisionError``) rather than overwrite; callers must
explicitly pass ``force=True`` (wired to the ``--force-backup`` CLI
flag) to overwrite a pre-existing same-day backup. This protects
against the common case of "I ran install twice today and the second
run silently clobbered the only good backup".

Backup ordering vs parse
------------------------
Callers MUST call ``load_json_safe(path)`` FIRST, then ``backup_file``
only when the load returned a usable dict. Backing up a known-corrupt
config and then writing it would leave the only "good" recovery
snapshot pointing at corrupt content; a later ``--force-backup`` run
would then overwrite the user's last known-good state with the
corrupt copy.

Public API
----------
  * ``NAMESPACE``                          — the metadata key string
  * ``is_our_namespace(group) -> bool``    — predicate used by both
                                             upsert and verifier
  * ``BackupCollisionError(Exception)``
  * ``backup_file(path, *, force=False) -> Path | None``
      Returns the backup path on success, or ``None`` if the source
      did not exist (first install).
  * ``atomic_write_text(path, text, *, encoding="utf-8") -> None``
  * ``atomic_write_json(path, payload, *, encoding="utf-8") -> None``
  * ``load_json_safe(path) -> (dict|None, str)``
      Returns ``(parsed_dict, "")`` on success. On failure, the
      second element is a stable error key (``"file_not_found"``,
      ``"unreadable: ..."``, ``"invalid_json: ..."``, or
      ``"not_an_object"``) suitable for ``json.loads`` output.
  * ``upsert_light_rip(config, group) -> dict``
      Insert/replace the Light RIP hook group into a Claude / Codex
      style ``hooks.UserPromptSubmit`` config, deduping ALL existing
      matching entries (not just the first).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


NAMESPACE = "light-rip-reminder"


def is_our_namespace(group: object) -> bool:
    """True iff ``group`` is a dict whose ``metadata.hook_namespace``
    equals our namespace. Used by both the upsert helper and the
    verifier to identify existing Light RIP entries."""
    if not isinstance(group, dict):
        return False
    metadata = group.get("metadata")
    return (isinstance(metadata, dict)
            and metadata.get("hook_namespace") == NAMESPACE)


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


# ---------- config load + upsert ----------

def load_json_safe(path: Path) -> tuple[dict | None, str]:
    """Return ``(parsed_dict, error_string)``.

    The second element is ``""`` on success. On failure it carries a
    stable error key suitable for surfacing in JSON output:

      ``"file_not_found"``  — ``path`` does not exist (first install;
                              callers should treat as success and
                              start from ``{}``).
      ``"unreadable: <msg>"`` — ``OSError`` on read (permission,
                              locked file, …).
      ``"invalid_json: <msg>"`` — ``json.JSONDecodeError`` (text
                              parsed, but the JSON is malformed).
      ``"not_an_object"``   — JSON parsed cleanly but the root is
                              not a JSON object (e.g. array or
                              scalar). We refuse to upsert into a
                              non-object root.

    The function NEVER raises. Callers must inspect the second
    element and decide whether to back up, abort, or proceed.
    """
    if not path.is_file():
        return None, "file_not_found"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid_json: {exc}"
    if not isinstance(parsed, dict):
        return None, "not_an_object"
    return parsed, ""


def upsert_light_rip(config: dict, group: dict) -> dict:
    """Insert or replace the Light RIP hook ``group`` in ``config``.

    Operates on the Claude / Codex ``hooks.UserPromptSubmit`` shape:

      config = {
        "hooks": {
          "UserPromptSubmit": [<group>, <group>, ...]
        },
        ...other top-level keys preserved
      }

    Behavior:

      * Ensures ``config["hooks"]`` exists as a dict and that
        ``config["hooks"]["UserPromptSubmit"]`` is a list. If a
        non-list value is found, replaces it with a fresh list (the
        schema requires an array).
      * Removes ALL existing entries whose ``metadata.hook_namespace``
        matches our namespace (handles duplicate entries from
        previous partial installs — see PR-A review finding R1).
      * Appends ``group`` to the end. The caller is responsible for
        setting ``group["metadata"]["hook_namespace"]`` so future
        upserts can identify it.
      * Mutates ``config`` in place AND returns it for convenience.
    """
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        config["hooks"] = hooks
    ups_list = hooks.get("UserPromptSubmit")
    if not isinstance(ups_list, list):
        ups_list = []
        hooks["UserPromptSubmit"] = ups_list
    # Dedupe ALL existing matching entries, not just the first.
    # A previous installer that crashed mid-write could have left two
    # half-finished entries; honoring only the last is the original
    # bug, but honoring only the first is also wrong. Strip both.
    ups_list[:] = [g for g in ups_list if not is_our_namespace(g)]
    ups_list.append(group)
    return config