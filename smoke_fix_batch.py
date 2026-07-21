"""Smoke regression for the stage-1 fix batch.

Exercises each blocker / high / new-correctness fix on isolated
fixture paths under a temporary directory so the smoke cannot
accidentally touch real ``~/.claude`` / ``~/.codex`` / ``~/.zcode``
configs.

Exit code 0 = all assertions passed.
Exit code 1 = at least one assertion failed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LIGHT_RIP = Path(__file__).resolve().parent
HOOKS = LIGHT_RIP / "hooks"
PYTHON = sys.executable


def run(cmd, **kw):
    """subprocess.run wrapper that captures output and raises on
    unexpected errors. Caller inspects .returncode / .stdout /
    .stderr."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def assert_eq(actual, expected, label):
    if actual == expected:
        print(f"  PASS  {label}: {actual!r}")
    else:
        print(f"  FAIL  {label}: got {actual!r}, expected {expected!r}")
        if isinstance(actual, str) and len(actual) < 400:
            print(f"        detail: {actual}")
        raise SystemExit(1)


def assert_true(cond, label):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        raise SystemExit(1)


def main():
    failures = 0
    with tempfile.TemporaryDirectory(prefix="light-rip-smoke-") as root:
        root_p = Path(root)
        # Independent subdirs so each test starts from a clean slate.
        claude_dir = root_p / "claude"
        codex_dir = root_p / "codex"
        zcode_dir = root_p / "zcode"
        for d in (claude_dir, codex_dir, zcode_dir):
            d.mkdir(parents=True, exist_ok=True)
        claude_settings = claude_dir / "settings.json"
        codex_hooks = codex_dir / "hooks.json"
        codex_config = codex_dir / "config.toml"
        zcode_config = zcode_dir / "config.json"

        # ----- installer_common unit tests -----
        print("\n[installer_common]")
        ic_test = f"""
import sys, json
sys.path.insert(0, r'{HOOKS}')
from installer_common import load_json_safe, upsert_light_rip
from pathlib import Path

# T1: missing file
r, e = load_json_safe(Path(r'{root_p}/does-not-exist'))
assert (r, e) == (None, 'file_not_found'), (r, e)

# T2: corrupt json
corrupt = Path(r'{root_p}/corrupt.json')
corrupt.write_text('{{ this is not valid json')
r, e = load_json_safe(corrupt)
assert e.startswith('invalid_json:'), e
assert r is None

# T3: array root (not dict)
arr = Path(r'{root_p}/arr.json')
arr.write_text('[1, 2, 3]')
r, e = load_json_safe(arr)
assert (r, e) == (None, 'not_an_object'), (r, e)

# T4: dedup all
cfg = {{'hooks': {{'UserPromptSubmit': [
    {{'metadata': {{'hook_namespace': 'light-rip-reminder'}}, 'hooks': [{{'old': 1}}]}},
    {{'metadata': {{'hook_namespace': 'light-rip-reminder'}}, 'hooks': [{{'old': 2}}]}},
    {{'metadata': {{'hook_namespace': 'light-rip-reminder'}}, 'hooks': [{{'old': 3}}]}},
]}}}}
new_group = {{'metadata': {{'hook_namespace': 'light-rip-reminder'}}, 'hooks': [{{'new': True}}]}}
upsert_light_rip(cfg, new_group)
ups_list = cfg['hooks']['UserPromptSubmit']
assert len(ups_list) == 1, len(ups_list)
assert ups_list[0]['hooks'] == [{{'new': True}}]

# T5: dedup preserves non-matching entries
cfg2 = {{'hooks': {{'UserPromptSubmit': [
    {{'metadata': {{'hook_namespace': 'other-tool'}}, 'hooks': [{{'keep': True}}]}},
    {{'metadata': {{'hook_namespace': 'light-rip-reminder'}}, 'hooks': [{{'old': True}}]}},
]}}}}
upsert_light_rip(cfg2, new_group)
ups_list2 = cfg2['hooks']['UserPromptSubmit']
assert len(ups_list2) == 2, len(ups_list2)
assert ups_list2[0]['metadata']['hook_namespace'] == 'other-tool'
assert ups_list2[1]['metadata']['hook_namespace'] == 'light-rip-reminder'

print('OK')
"""
        r = run([PYTHON, "-c", ic_test])
        assert_eq(r.returncode, 0, "installer_common unit tests")
        if r.stdout.strip():
            print("        " + r.stdout.strip())

        # ----- install_claude_hook.py -----
        print("\n[install_claude_hook.py]")

        # T5: first install
        r = run([
            PYTHON, str(HOOKS / "install_claude_hook.py"),
            "--settings-file", str(claude_settings),
            "--no-strict-verify",  # skip verifier subprocess for this test
        ])
        assert_eq(r.returncode, 0, "first install")
        settings = json.loads(claude_settings.read_text(encoding="utf-8-sig"))
        n = sum(1 for g in settings["hooks"]["UserPromptSubmit"]
                if g.get("metadata", {}).get("hook_namespace") == "light-rip-reminder")
        assert_eq(n, 1, "first install wrote exactly 1 entry")

        # T6: re-install (dedup)
        r = run([
            PYTHON, str(HOOKS / "install_claude_hook.py"),
            "--settings-file", str(claude_settings),
            "--no-strict-verify",
        ])
        assert_eq(r.returncode, 0, "re-install")
        settings = json.loads(claude_settings.read_text(encoding="utf-8-sig"))
        n = sum(1 for g in settings["hooks"]["UserPromptSubmit"]
                if g.get("metadata", {}).get("hook_namespace") == "light-rip-reminder")
        assert_eq(n, 1, "re-install still exactly 1 entry (dedup)")

        # T7: corrupt source -> exit 2 with invalid_runtime_config,
        # NO backup created (parse-before-backup contract)
        corrupt = claude_dir / "settings-corrupt.json"
        corrupt.write_text("{ this is not valid json")
        baks_before = list(claude_dir.glob("settings-corrupt.json.bak-*"))
        r = run([
            PYTHON, str(HOOKS / "install_claude_hook.py"),
            "--settings-file", str(corrupt),
            "--no-strict-verify",
        ])
        assert_eq(r.returncode, 2, "corrupt source returns 2")
        try:
            j = json.loads(r.stderr.strip().splitlines()[-1])
        except Exception:
            j = {}
        assert_eq(j.get("error"), "invalid_runtime_config",
                  "corrupt source reports error=invalid_runtime_config")
        baks_after = list(claude_dir.glob("settings-corrupt.json.bak-*"))
        assert_eq(len(baks_after), len(baks_before),
                  "no backup created for corrupt source (parse-before-backup)")

        # T8: collision on a known-good source
        col_dir = root_p / "collision"
        col_dir.mkdir(parents=True, exist_ok=True)
        col_target = col_dir / "settings.json"
        # Pre-populate the source so the FIRST install backs it up
        # (first install on a missing source creates no backup; we
        # need a backup from install 1 to collide with install 2).
        col_target.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                              encoding="utf-8")
        # First install -> creates backup with today's UTC date
        r1 = run([
            PYTHON, str(HOOKS / "install_claude_hook.py"),
            "--settings-file", str(col_target),
            "--no-strict-verify",
        ])
        assert_eq(r1.returncode, 0, "collision: first install ok")
        assert_true(any(col_target.with_suffix(
            col_target.suffix + ".bak-").glob("*.bak-*")
            for _ in [None]) if False else bool(
                list(col_target.parent.glob(col_target.name + ".bak-*"))
            ),
            "collision: first install created a .bak-* sibling")
        # Re-install without --force-backup -> collision
        r2 = run([
            PYTHON, str(HOOKS / "install_claude_hook.py"),
            "--settings-file", str(col_target),
            "--no-strict-verify",
        ])
        assert_eq(r2.returncode, 2, "collision: second install returns 2")
        try:
            j2 = json.loads(r2.stderr.strip().splitlines()[-1])
        except Exception:
            j2 = {}
        assert_eq(j2.get("error"), "backup_collision",
                  "collision reports error=backup_collision")
        assert_true("backup_path" in j2,
                    "collision response carries backup_path for user decision")

        # ----- install_codex_hook.py -----
        print("\n[install_codex_hook.py]")
        # T9: first install
        r = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(codex_hooks),
            "--config-file", str(codex_config),
            "--no-strict-verify",
        ])
        assert_eq(r.returncode, 0, "codex first install")
        hooks = json.loads(codex_hooks.read_text(encoding="utf-8-sig"))
        n = sum(1 for g in hooks["hooks"]["UserPromptSubmit"]
                if g.get("metadata", {}).get("hook_namespace") == "light-rip-reminder")
        assert_eq(n, 1, "codex first install wrote 1 entry")
        config_text = codex_config.read_text(encoding="utf-8-sig")
        assert_true("[features]" in config_text and "hooks = true" in config_text,
                    "codex config.toml has [features] hooks = true")

        # ----- verify_install.py -----
        print("\n[verify_install.py]")

        # T10: Claude install -> exit 0
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "claude",
            "--claude-settings", str(claude_settings),
            "--json",
        ])
        assert_eq(r.returncode, 0, "verify_install claude ok")
        try:
            j = json.loads(r.stdout)
            rc = j.get("runtime_checks", {}).get("claude", {})
            assert_eq(rc.get("installed"), True,
                      "claude runtime_checks.installed == True")
        except Exception as exc:
            print(f"        json parse failed: {exc}\n{r.stdout[:300]}")

        # T11: Codex 1 valid hook but NO config.toml -> exit 1 (R5b)
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "codex",
            "--codex-hooks", str(codex_hooks),
            "--codex-config", str(codex_dir / "config-missing.toml"),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install codex (missing config.toml) returns 1")
        j = json.loads(r.stdout)
        rc = j.get("runtime_checks", {}).get("codex", {})
        assert_eq(rc.get("installed"), False,
                  "codex runtime_checks.installed == False")
        assert_eq(rc.get("match_count"), 1,
                  "codex runtime_checks.match_count == 1")

        # T12: ZCode with hooks.enabled == false -> exit 1 (R7)
        bad_zcode = zcode_dir / "bad-enabled.json"
        bad_zcode.write_text(json.dumps({
            "hooks": {
                "enabled": False,
                "events": {
                    "UserPromptSubmit": [{
                        "hooks": [{
                            "type": "process",
                            "command": str(Path(sys.executable)),
                            "args": [str(HOOKS / "light_rip_reminder.py"),
                                     "--format", "harness"],
                        }]
                    }]
                }
            }
        }))
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(bad_zcode),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install zcode (hooks.enabled=false) returns 1")
        j = json.loads(r.stdout)
        rc = j.get("runtime_checks", {}).get("zcode", {})
        assert_eq(rc.get("installed"), False,
                  "zcode (enabled=false) installed == False")

        # T13: ZCode with type="command" (wrong type) -> exit 1 (R7)
        wrong_type = zcode_dir / "wrong-type.json"
        wrong_type.write_text(json.dumps({
            "hooks": {
                "enabled": True,
                "events": {
                    "UserPromptSubmit": [{
                        "hooks": [{
                            "type": "command",
                            "command": str(Path(sys.executable)),
                            "args": [str(HOOKS / "light_rip_reminder.py"),
                                     "--format", "harness"],
                        }]
                    }]
                }
            }
        }))
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(wrong_type),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install zcode (type=command) returns 1")
        j = json.loads(r.stdout)
        rc = j.get("runtime_checks", {}).get("zcode", {})
        assert_eq(rc.get("match_count"), 0,
                  "zcode (type=command) match_count == 0")

        # T14: ZCode with non-python .exe command -> exit 1 (R7)
        non_python = zcode_dir / "non-python.json"
        non_python.write_text(json.dumps({
            "hooks": {
                "enabled": True,
                "events": {
                    "UserPromptSubmit": [{
                        "hooks": [{
                            "type": "process",
                            "command": "C:/Windows/System32/node.exe",
                            "args": [str(HOOKS / "light_rip_reminder.py"),
                                     "--format", "harness"],
                        }]
                    }]
                }
            }
        }))
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(non_python),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install zcode (node.exe) returns 1")
        j = json.loads(r.stdout)
        rc = j.get("runtime_checks", {}).get("zcode", {})
        assert_eq(rc.get("match_count"), 0,
                  "zcode (node.exe) match_count == 0")

        # T15: ZCode with valid entry -> exit 0
        good_zcode = zcode_dir / "good.json"
        good_zcode.write_text(json.dumps({
            "hooks": {
                "enabled": True,
                "events": {
                    "UserPromptSubmit": [{
                        "hooks": [{
                            "type": "process",
                            "command": str(Path(sys.executable)),
                            "args": [str(HOOKS / "light_rip_reminder.py"),
                                     "--format", "harness"],
                        }]
                    }]
                }
            }
        }))
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(good_zcode),
            "--json",
        ])
        assert_eq(r.returncode, 0, "verify_install zcode (valid) returns 0")
        j = json.loads(r.stdout)
        rc = j.get("runtime_checks", {}).get("zcode", {})
        assert_eq(rc.get("installed"), True,
                  "zcode (valid) installed == True")

        # T16: ZCode corrupt branch carries backup hint (R2)
        bad_zcode_corrupt = zcode_dir / "corrupt.json"
        bad_zcode_corrupt.write_text("{ not valid json")
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(bad_zcode_corrupt),
            "--json",
        ])
        assert_eq(r.returncode, 2, "verify_install zcode (corrupt) returns 2")
        j = json.loads(r.stdout)
        rc = j.get("runtime_checks", {}).get("zcode", {})
        # Even with no backup present yet, the message should not crash
        # and should mention 'corrupt' so the user knows what happened.
        detail = rc.get("detail", "")
        assert_true("corrupt" in detail,
                    "zcode corrupt branch mentions 'corrupt'")

        print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()