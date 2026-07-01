#!/usr/bin/env python3
"""Install the Light RIP reminder as a UserPromptSubmit hook for any agent.

The "general" installer covers agent runtimes that are not the original
two (Claude Code / Codex). Those two keep their dedicated installers
(`install_claude_hook.py` / `install_codex_hook.py`) because they
shipped first and the call shape is hardcoded into their config files.

The general installer writes a Mavis-style hook file (markdown with
frontmatter + fenced bash block) into the target agent's hooks
directory. The body runs `light_rip_reminder.py --format <format>`,
where `<format>` selects the correct JSON envelope for that agent.

Defaults target Mavis / Mavis Code (the primary "other agent" use
case). Override flags to install for any other runtime that uses the
same hook-file convention.

Windows note
------------
The Mavis hook runner executes the body of a fenced `bash` block
through `sh` on Windows. Git for Windows ships `sh.exe` at
`C:\\Program Files\\Git\\bin\\sh.exe`, but it is not on PATH by default.
The installer detects this and appends Git Bash to the user's PATH via
the registry (with a `SendMessageTimeout` broadcast so new processes
pick it up immediately).

The currently running Mavis daemon keeps the PATH it was launched
with. **Restart the agent runtime** after installing so the new PATH
takes effect for the hook runner. Pass `--no-path-fix` if you prefer
to manage PATH yourself.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import winreg  # type: ignore[import-not-found]
from pathlib import Path

NAMESPACE = "light-rip-reminder"
HOOK_FILE_NAME = f"{NAMESPACE}.md"

# Default target = Mavis. The "general" name reflects the script's
# flexibility, not that it is agent-agnostic on disk — every agent
# runtime has its own hooks directory layout, so we still need a
# concrete default.
DEFAULT_AGENT = "mavis"
DEFAULT_DATA_DIR = None  # resolved from $MAVIS_DATA_DIR or ~/.mavis
DEFAULT_FORMAT = "mavis"
DEFAULT_EVENT = "UserPromptSubmit"
DEFAULT_PRIORITY = 5
DEFAULT_TIMEOUT_MS = 5000

# Common Git-for-Windows install locations to probe when sh is missing.
GIT_BASH_CANDIDATES = [
    Path(r"C:\Program Files\Git\bin"),
    Path(r"C:\Program Files (x86)\Git\bin"),
]


# ---------- path resolution ----------

def mavis_data_dir() -> Path:
    return Path(os.environ.get("MAVIS_DATA_DIR") or Path.home() / ".mavis")


def resolve_hooks_dir(args: argparse.Namespace) -> Path:
    if args.hooks_dir:
        return args.hooks_dir.expanduser().resolve()
    data_dir = (args.data_dir.expanduser().resolve() if args.data_dir
                else mavis_data_dir())
    return data_dir / "agents" / args.agent / "hooks"


# ---------- hook file generation ----------

def build_hook_file(python_exe: Path, hook_script: Path,
                    event: str, format_name: str,
                    priority: int, timeout_ms: int) -> str:
    """Build a Mavis-style hook definition (markdown + frontmatter)."""
    # Forward slashes inside the command — Python handles both, and
    # forward slashes avoid backslash-escape headaches in the markdown
    # fence. sh will pass the path through verbatim to python.
    py = str(python_exe).replace("\\", "/")
    script = str(hook_script).replace("\\", "/")
    body = (
        "---\n"
        f"hookEvent: {event}\n"
        f"type: script\n"
        f"priority: {priority}\n"
        f"timeout: {timeout_ms}\n"
        f"matcher: \"\"\n"
        "---\n"
        "\n"
        "```bash\n"
        f"\"{py}\" \"{script}\" --format {format_name}\n"
        "```\n"
    )
    return body


def upsert_hook_file(hooks_dir: Path, body: str) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / HOOK_FILE_NAME
    target.write_text(body, encoding="utf-8")
    return target


# ---------- Windows sh / Git Bash detection ----------

def sh_on_path() -> bool:
    return shutil.which("sh") is not None


def find_git_bash_bin() -> Path | None:
    for cand in GIT_BASH_CANDIDATES:
        if cand and (cand / "sh.exe").is_file():
            return cand
    return None


def add_to_user_path(bin_dir: Path) -> tuple[bool, str]:
    """Append bin_dir to the user's persistent PATH. Returns (changed, message)."""
    if not isinstance(bin_dir, Path):
        bin_dir = Path(bin_dir)
    bin_str = str(bin_dir)

    # Read current user PATH directly from the registry so we know
    # exactly what we'd be appending to (the live process PATH may
    # differ from the persistent one).
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment",
                            0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                existing, _ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                existing = ""
    except OSError as exc:
        return False, f"could not read user PATH: {exc}"

    if bin_str.lower() in existing.lower():
        return False, f"{bin_str} already on user PATH"

    new_path = f"{existing};{bin_str}" if existing else bin_str
    if len(new_path) > 1024:
        # setx truncates at 1024 chars. Drop oldest entries first.
        parts = [p for p in existing.split(";") if p]
        while parts and len(";".join(parts) + ";" + bin_str) > 1024:
            parts.pop(0)
        new_path = (";".join(parts) + ";" + bin_str) if parts else bin_str

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment",
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
    except OSError as exc:
        return False, f"could not write user PATH: {exc}"

    # Broadcast WM_SETTINGCHANGE so new processes pick it up without
    # requiring a sign-out.
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Add-Type -Namespace W -Name B -MemberDefinition '[DllImport(\"user32.dll\")] public static extern int SendMessageTimeout(IntPtr hWnd, uint Msg, IntPtr wParam, string lParam, uint flags, uint timeout, out int result);'; "
             "[W.B]::SendMessageTimeout([IntPtr]0xFFFF, 0x001A, [IntPtr]0, \"Environment\", 2, 5000, [ref]0) | Out-Null"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass
    return True, f"added {bin_str} to user PATH (restart the agent runtime to take effect)"


# ---------- entry point ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the Light RIP reminder as a UserPromptSubmit hook "
                    "for a general agent runtime (defaults to Mavis)."
    )
    parser.add_argument("--agent", default=os.environ.get("LIGHT_RIP_AGENT", DEFAULT_AGENT),
                        help=f"Target agent name (default: {DEFAULT_AGENT}).")
    parser.add_argument("--data-dir", type=Path,
                        help="Agent data dir (default: $MAVIS_DATA_DIR or ~/.mavis).")
    parser.add_argument("--hooks-dir", type=Path,
                        help="Override hooks directory directly. Skips data-dir/agent resolution.")
    parser.add_argument("--format", default=os.environ.get("LIGHT_RIP_FORMAT", DEFAULT_FORMAT),
                        choices=["harness", "mavis"],
                        help="Reminder output format (default: mavis).")
    parser.add_argument("--event", default=DEFAULT_EVENT,
                        help=f"Hook event name (default: {DEFAULT_EVENT}).")
    parser.add_argument("--priority", type=int, default=DEFAULT_PRIORITY,
                        help=f"Hook priority (lower runs first; default {DEFAULT_PRIORITY}).")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS,
                        help=f"Hook timeout in ms (default {DEFAULT_TIMEOUT_MS}).")
    parser.add_argument("--no-path-fix", action="store_true",
                        help="Do not attempt to add Git Bash to user PATH on Windows.")
    args = parser.parse_args()

    hook_script = Path(__file__).resolve().parent / "light_rip_reminder.py"
    if not hook_script.exists():
        print(json.dumps({"error": f"reminder script not found at {hook_script}"}))
        return 1

    hooks_dir = resolve_hooks_dir(args)
    python_exe = Path(sys.executable).resolve()

    body = build_hook_file(
        python_exe=python_exe,
        hook_script=hook_script,
        event=args.event,
        format_name=args.format,
        priority=args.priority,
        timeout_ms=args.timeout,
    )
    target = upsert_hook_file(hooks_dir, body)

    result = {
        "hook_file": str(target),
        "agent": args.agent,
        "format": args.format,
        "event": args.event,
        "namespace": NAMESPACE,
    }

    # Windows PATH check: the hook runner needs `sh` to execute the
    # bash block. If it's missing, try to add Git Bash to user PATH.
    if os.name == "nt" and not sh_on_path() and not args.no_path_fix:
        git_bin = find_git_bash_bin()
        if git_bin is not None:
            changed, msg = add_to_user_path(git_bin)
            result["path_fix"] = {
                "attempted": True, "changed": changed, "message": msg,
                "note": "Restart the agent runtime for the new PATH to take effect.",
            }
        else:
            result["path_fix"] = {
                "attempted": False,
                "message": "Git Bash not found in standard locations; install Git for Windows or ensure `sh` is on PATH.",
            }
    elif os.name == "nt" and not sh_on_path() and args.no_path_fix:
        result["path_fix"] = {"attempted": False, "message": "--no-path-fix set; ensure sh is on the runtime's PATH manually."}

    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
