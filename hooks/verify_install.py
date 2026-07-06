#!/usr/bin/env python3
"""Verify a Light RIP install.

Run after `install_general_agent_hook.py` (or after a dedicated
installer) to confirm the reminder hook landed correctly. Each
supported agent has its own check list; unknown agents fall back to
generic health checks.

Exit codes:
  0 — all checks passed
  1 — at least one agent-specific check failed
  2 — setup is wrong (e.g. reminder.md missing) — caller should fix
      and re-run

Output:
  - default: human-readable multi-line summary
  - --json : single JSON object on stdout
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

NAMESPACE = "light-rip-reminder"


# ---------- shared helpers ----------

def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def reminder_md_path() -> Path:
    return skill_root() / "reminder.md"


def reminder_script_path() -> Path:
    return skill_root() / "hooks" / "light_rip_reminder.py"


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def check(label: str, ok: bool, detail: str = "") -> dict:
    return {"name": label, "ok": bool(ok), "detail": detail}


def aggregate(checks: list[dict]) -> bool:
    return all(c["ok"] for c in checks)


# ---------- common prerequisite checks ----------

def common_checks() -> list[dict]:
    rm = reminder_md_path()
    rs = reminder_script_path()
    return [
        check("reminder.md exists", rm.is_file(), str(rm)),
        check("reminder script exists", rs.is_file(), str(rs)),
        check(
            "reminder script runnable",
            rs.is_file(),
            f"`python {rs} --help` should succeed",
        ),
    ]


# ---------- per-agent checks ----------

def _extract_command_str(entry: dict) -> str | None:
    """Pull a human-readable command string from a hook entry."""
    cmd = entry.get("command")
    if isinstance(cmd, str):
        return cmd
    # ZCode-style process entry: command + args
    base = entry.get("command")
    args = entry.get("args") or []
    if isinstance(base, str):
        return " ".join([base] + [str(a) for a in args])
    return None


def _command_references_reminder(cmd: str) -> bool:
    return ("light_rip_reminder.py" in cmd) or ("light-rip-reminder" in cmd)


def check_codex() -> list[dict]:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    hooks_file = codex_home / "hooks.json"
    config_file = codex_home / "config.toml"
    out: list[dict] = []

    data = load_json(hooks_file)
    if data is None:
        return [check(f"{hooks_file} present", False, "missing or invalid JSON")]

    user_prompt = (
        (data.get("hooks") or {}).get("UserPromptSubmit") or []
    )
    if not isinstance(user_prompt, list):
        return [check("UserPromptSubmit list", False, "shape mismatch")]

    found = None
    for group in user_prompt:
        if not isinstance(group, dict):
            continue
        metadata = group.get("metadata") or {}
        if (isinstance(metadata, dict)
                and metadata.get("hook_namespace") == NAMESPACE):
            inner = group.get("hooks") or []
            if inner:
                found = inner[0]
                break

    if found is None:
        out.append(check(
            "codex hook entry present",
            False,
            f"no group with metadata.hook_namespace=={NAMESPACE!r} under "
            f"hooks.UserPromptSubmit",
        ))
        return out

    cmd = _extract_command_str(found) or ""
    out.append(check("codex hook entry present", True, str(hooks_file)))
    out.append(check(
        "codex command references reminder script",
        _command_references_reminder(cmd),
        cmd or "<empty>",
    ))

    if config_file.is_file():
        try:
            text = config_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            out.append(check("config.toml readable", False, str(exc)))
            return out
        in_features = re.search(r"^\[features\]\s*$", text, re.M) is not None
        hooks_true = bool(re.search(
            r"^\s*hooks\s*=\s*true\b", text, re.M,
        ))
        out.append(check(
            "codex config.toml has [features] hooks = true",
            in_features and hooks_true,
            str(config_file),
        ))
    else:
        out.append(check(
            "codex config.toml present",
            False,
            f"missing (hooks feature may not be enabled): {config_file}",
        ))

    return out


def check_claude() -> list[dict]:
    settings = Path.home() / ".claude" / "settings.json"
    data = load_json(settings)
    if data is None:
        return [check(f"{settings} present", False, "missing or invalid JSON")]

    user_prompt = (data.get("hooks") or {}).get("UserPromptSubmit") or []
    if not isinstance(user_prompt, list):
        return [check("UserPromptSubmit list", False, "shape mismatch")]

    found = None
    for group in user_prompt:
        if not isinstance(group, dict):
            continue
        metadata = group.get("metadata") or {}
        if (isinstance(metadata, dict)
                and metadata.get("hook_namespace") == NAMESPACE):
            inner = group.get("hooks") or []
            if inner:
                found = inner[0]
                break

    if found is None:
        return [check(
            "claude hook entry present",
            False,
            f"no group with metadata.hook_namespace=={NAMESPACE!r}",
        )]

    cmd = _extract_command_str(found) or ""
    return [
        check("claude hook entry present", True, str(settings)),
        check(
            "claude command references reminder script",
            _command_references_reminder(cmd),
            cmd or "<empty>",
        ),
    ]


def check_zcode() -> list[dict]:
    config = Path.home() / ".zcode" / "config.json"
    cli_config = Path.home() / ".zcode" / "cli" / "config.json"
    out: list[dict] = []

    data = load_json(config)
    if data is None:
        out.append(check(f"{config} present", False, "missing or invalid JSON"))
        # Fall back to CLI config — ZCode CLI reads cli/config.json.
        data = load_json(cli_config)
        if data is None:
            return out
        out.append(check(f"{cli_config} present (fallback)", True,
                         f"using {cli_config}"))

    hooks = (data.get("hooks") or {})
    if not isinstance(hooks, dict):
        return out + [check("config.json hooks block", False, "not a dict")]

    enabled = hooks.get("enabled") is True
    out.append(check("config.json hooks.enabled = true", enabled,
                     f"current: {hooks.get('enabled')!r}"))

    events = hooks.get("events") or {}
    if not isinstance(events, dict):
        return out + [check("config.json hooks.events", False, "not a dict")]

    user_prompt = events.get("UserPromptSubmit") or []
    if not isinstance(user_prompt, list) or not user_prompt:
        return out + [check(
            "config.json hooks.events.UserPromptSubmit present", False,
            "missing or empty",
        )]

    # Find at least one process entry whose command references light-rip.
    matched = False
    matched_cmd = ""
    for group in user_prompt:
        if not isinstance(group, dict):
            continue
        inner = group.get("hooks") or []
        for entry in inner:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "process":
                continue
            cmd = _extract_command_str(entry) or ""
            if _command_references_reminder(cmd):
                matched = True
                matched_cmd = cmd
                break
        if matched:
            break

    out.append(check(
        "config.json hooks.events.UserPromptSubmit has process entry "
        "referencing light_rip_reminder.py",
        matched,
        matched_cmd or "<no matching entry>",
    ))
    return out


def check_mavis() -> list[dict]:
    data_dir = Path(
        os.environ.get("MAVIS_DATA_DIR") or Path.home() / ".mavis"
    )
    hook_file = data_dir / "agents" / "mavis" / "hooks" / f"{NAMESPACE}.md"
    out: list[dict] = []

    if not hook_file.is_file():
        return [check(f"{hook_file} present", False, "missing")]

    text = hook_file.read_text(encoding="utf-8")
    frontmatter_match = re.match(
        r"^---\s*\n(?P<body>.*?)\n---\s*\n",
        text, re.S,
    )
    if not frontmatter_match:
        return out + [check("mavis frontmatter present", False,
                            "no leading --- block")]

    fm = frontmatter_match.group("body")
    has_event = re.search(r"^hookEvent:\s*UserPromptSubmit\b", fm, re.M) is not None
    out.append(check("mavis hookEvent = UserPromptSubmit", has_event, hook_file.name))

    body = text[frontmatter_match.end():]
    references = _command_references_reminder(body)
    out.append(check(
        "mavis body references reminder script",
        references,
        "fenced bash block missing reminder script invocation",
    ))
    return out


def check_opencode() -> list[dict]:
    cfg = Path.home() / ".config" / "opencode" / "opencode.json"
    data = load_json(cfg)
    if data is None:
        return [check(f"{cfg} present", False, "missing or invalid JSON")]

    instructions = data.get("instructions")
    if not isinstance(instructions, list) or not instructions:
        return [check("opencode instructions list", False, "empty or missing")]

    rm = str(reminder_md_path())
    matched = any(
        isinstance(s, str) and (s == rm or s.endswith("reminder.md"))
        for s in instructions
    )
    return [
        check(f"{cfg} present", True, str(cfg)),
        check("opencode instructions includes reminder.md", matched,
              str(instructions)),
    ]


def check_skill_only() -> list[dict]:
    """Verify a skill-only install (Section C fallback)."""
    candidates = [
        Path.home() / ".agents" / "skills" / "light-rip" / "SKILL.md",
        Path.home() / ".claude" / "skills" / "light-rip" / "SKILL.md",
        Path.home() / ".codex" / "skills" / "light-rip" / "SKILL.md",
    ]
    found = [p for p in candidates if p.is_file()]
    if not found:
        return [check(
            "skill-only: SKILL.md discoverable",
            False,
            "not found under any of: " + ", ".join(str(p) for p in candidates),
        )]
    return [check(
        "skill-only: SKILL.md discoverable",
        True,
        ", ".join(str(p) for p in found),
    )]


def check_generic() -> list[dict]:
    return common_checks()


# ---------- dispatch ----------

CHECKERS: dict[str, Callable[[], list[dict]]] = {
    "codex": check_codex,
    "claude": check_claude,
    "claude-code": check_claude,
    "zcode": check_zcode,
    "mavis": check_mavis,
    "opencode": check_opencode,
    "generic": check_generic,
    "unknown": check_generic,
}


def run_checks(agent: str) -> list[dict]:
    checker = CHECKERS.get(agent.lower(), check_generic)
    agent_checks = checker()
    return common_checks() + agent_checks


# ---------- output ----------

def render_human(agent: str, checks: list[dict]) -> str:
    ok = aggregate(checks)
    header = f"Light RIP install verification — agent={agent} — {'PASS' if ok else 'FAIL'}"
    lines = [header, ""]
    for c in checks:
        marker = "OK  " if c["ok"] else "FAIL"
        detail = f" — {c['detail']}" if c["detail"] else ""
        lines.append(f"  [{marker}] {c['name']}{detail}")
    return "\n".join(lines) + "\n"


def render_json(agent: str, checks: list[dict]) -> str:
    return json.dumps(
        {"agent": agent, "ok": aggregate(checks), "checks": checks},
        indent=2, ensure_ascii=True,
    )


# ---------- entry point ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Light RIP install for the named agent.",
    )
    parser.add_argument("--agent", required=True,
                        help="Agent name (codex, claude, zcode, mavis, "
                             "opencode, generic, ...).")
    parser.add_argument("--target-runtime",
                        help="Ignored — kept for parity with "
                             "install_general_agent_hook.py.")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human-readable text.")
    args = parser.parse_args()

    # Setup-level pre-check: if the skill itself is broken, return 2.
    if not reminder_md_path().is_file():
        msg = f"setup wrong: {reminder_md_path()} missing"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 2

    checks = run_checks(args.agent)
    if args.json:
        print(render_json(args.agent, checks))
    else:
        print(render_human(args.agent, checks), end="")
    return 0 if aggregate(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())