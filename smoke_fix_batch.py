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

        # ----- PR-C: PEP 394/397 launcher predicate -----
        print("\n[PR-C: ZCode detector Python-launcher predicate]")
        pr_c_dir = root_p / "pr-c"
        pr_c_dir.mkdir(parents=True, exist_ok=True)
        py_exe_path = Path(sys.executable)
        reminder_path = HOOKS / "light_rip_reminder.py"
        reminder_path_str = str(reminder_path)

        def make_zcode_config(name: str, command: str) -> Path:
            cfg = pr_c_dir / name
            cfg.write_text(json.dumps({
                "hooks": {
                    "enabled": True,
                    "events": {
                        "UserPromptSubmit": [{
                            "hooks": [{
                                "type": "process",
                                "command": command,
                                "args": [reminder_path_str,
                                         "--format", "harness"],
                            }]
                        }]
                    }
                }
            }))
            return cfg

        # Positive: bare `py` (Windows launcher name)
        cfg_py = make_zcode_config("py.json", "py")
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_py),
            "--json",
        ])
        assert_eq(r.returncode, 0, "verify_install zcode (py launcher) returns 0")
        rc = json.loads(r.stdout)["runtime_checks"]["zcode"]
        assert_eq(rc.get("installed"), True,
                  "verify_install zcode (py) installed == True")

        # Positive: py.exe
        cfg_py_exe = make_zcode_config("py-exe.json", "py.exe")
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_py_exe),
            "--json",
        ])
        assert_eq(r.returncode, 0, "verify_install zcode (py.exe) returns 0")

        # Positive: full Windows path with py.exe
        cfg_py_path = make_zcode_config(
            "py-path.json", r"C:\Python312\py.exe"
        )
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_py_path),
            "--json",
        ])
        assert_eq(r.returncode, 0, "verify_install zcode (py.exe path) returns 0")

        # Positive: python3.12.exe (versioned interpreter)
        cfg_py312 = make_zcode_config(
            "python312.json", r"C:\Python312\python3.12.exe"
        )
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_py312),
            "--json",
        ])
        assert_eq(r.returncode, 0, "verify_install zcode (python3.12.exe) returns 0")

        # Negative: pythonw.exe (GUI launcher, should NOT match)
        cfg_pyw = make_zcode_config("pythonw.json", r"C:\Python312\pythonw.exe")
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_pyw),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install zcode (pythonw.exe) returns 1")
        rc = json.loads(r.stdout)["runtime_checks"]["zcode"]
        assert_eq(rc.get("match_count"), 0,
                  "verify_install zcode (pythonw.exe) match_count == 0")

        # Negative: mypython.exe (false positive under old predicate)
        cfg_mypy = make_zcode_config("mypython.json", "mypython.exe")
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_mypy),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install zcode (mypython.exe) returns 1")
        rc = json.loads(r.stdout)["runtime_checks"]["zcode"]
        assert_eq(rc.get("match_count"), 0,
                  "verify_install zcode (mypython.exe) match_count == 0")

        # Negative: python3.12-config (dev tool, not interpreter)
        cfg_pycfg = make_zcode_config("python312-config.json",
                                       "python3.12-config")
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_pycfg),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install zcode (python3.12-config) returns 1")

        # Negative: python_d.exe (debug build)
        cfg_pyd = make_zcode_config("python_d.json", "python_d.exe")
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "zcode",
            "--zcode-config", str(cfg_pyd),
            "--json",
        ])
        assert_eq(r.returncode, 1, "verify_install zcode (python_d.exe) returns 1")

        # ----- PR-D: config.toml idempotent + preservation -----
        print("\n[PR-D: Codex config.toml via tomllib/tomli_w]")

        pr_d_dir = root_p / "pr-d"
        pr_d_dir.mkdir(parents=True, exist_ok=True)

        # T-PR-D-1: pre-existing config.toml with hooks=true and
        # comments → re-install must NOT rewrite → byte-equal.
        cfg1 = pr_d_dir / "idempotent.toml"
        cfg1_original = (
            "# my custom settings\n"
            "[features]\n"
            "hooks = true  # already enabled\n"
            "[mcp]\n"
            'servers = ["foo"]\n'
        )
        cfg1.write_text(cfg1_original, encoding="utf-8")
        # Need an existing hooks.json alongside for the installer.
        hooks1 = pr_d_dir / "idempotent-hooks.json"
        hooks1.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                          encoding="utf-8")
        r = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(hooks1),
            "--config-file", str(cfg1),
            "--no-strict-verify",
        ])
        assert_eq(r.returncode, 0, "PR-D idempotent re-install ok")
        # File must be byte-equal after re-install (no rewrite when
        # hooks already true).
        assert_eq(cfg1.read_text(encoding="utf-8"), cfg1_original,
                  "PR-D: hooks=true re-install is byte-equal (no comment loss)")
        # No backup should have been created either.
        baks = list(pr_d_dir.glob("idempotent.toml.bak-*"))
        assert_eq(len(baks), 0,
                  "PR-D: hooks=true re-install creates no backup")

        # T-PR-D-2: config.toml with hooks=false → must rewrite to true.
        # [mcp] and [plugins] sections must survive.
        cfg2 = pr_d_dir / "rewrite.toml"
        cfg2_original = (
            "# header comment\n"
            "[features]\n"
            "goals = true\n"
            "hooks = false\n"
            "[mcp]\n"
            'servers = ["foo", "bar"]\n'
            "[plugins]\n"
            'name = "x"\n'
        )
        cfg2.write_text(cfg2_original, encoding="utf-8")
        hooks2 = pr_d_dir / "rewrite-hooks.json"
        hooks2.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                          encoding="utf-8")
        r = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(hooks2),
            "--config-file", str(cfg2),
            "--no-strict-verify",
        ])
        assert_eq(r.returncode, 0, "PR-D hooks=false rewrite ok")
        # Read back and verify features.hooks == True
        import tomllib as _tomllib
        with open(cfg2, "rb") as f:
            data = _tomllib.load(f)
        assert_eq(data.get("features", {}).get("hooks"), True,
                  "PR-D: hooks=false rewritten to true")
        # [mcp] and [plugins] sections preserved
        assert_true(isinstance(data.get("mcp"), dict)
                    and data["mcp"].get("servers") == ["foo", "bar"],
                    "PR-D: [mcp] section preserved")
        assert_true(isinstance(data.get("plugins"), dict)
                    and data["plugins"].get("name") == "x",
                    "PR-D: [plugins] section preserved")
        # Backup WAS made (because write was needed)
        baks = list(pr_d_dir.glob("rewrite.toml.bak-*"))
        assert_eq(len(baks), 1,
                  "PR-D: hooks=false rewrite creates exactly 1 backup")

        # T-PR-D-3: empty config.toml → installer creates [features] hooks=true.
        cfg3 = pr_d_dir / "empty.toml"
        # Don't create the file — installer should write it fresh.
        hooks3 = pr_d_dir / "empty-hooks.json"
        hooks3.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                          encoding="utf-8")
        r = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(hooks3),
            "--config-file", str(cfg3),
            "--no-strict-verify",
        ])
        assert_eq(r.returncode, 0, "PR-D empty config.toml install ok")
        assert_true(cfg3.exists(), "PR-D: empty config.toml was created")
        with open(cfg3, "rb") as f:
            data = _tomllib.load(f)
        assert_eq(data.get("features", {}).get("hooks"), True,
                  "PR-D: empty config.toml gets features.hooks = true")

        # T-PR-D-4: hooks = "true" (quoted) — both TOML branch and
        # regex fallback should agree the feature is enabled.
        cfg4 = pr_d_dir / "quoted.toml"
        cfg4.write_text('[features]\nhooks = "true"\n', encoding="utf-8")
        hooks4 = pr_d_dir / "quoted-hooks.json"
        # Pre-create hooks.json with a valid Light RIP entry so the
        # verifier sees installed=True.
        hooks4.write_text(json.dumps({
            "hooks": {
                "UserPromptSubmit": [{
                    "metadata": {
                        "hook_namespace": "light-rip-reminder",
                    },
                    "hooks": [{"type": "command", "command": "echo"}],
                }]
            }
        }), encoding="utf-8")
        r = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(hooks4),
            "--config-file", str(cfg4),
            "--no-strict-verify",
        ])
        # Should succeed; hooks = "true" is already enabled so no write
        assert_eq(r.returncode, 0, "PR-D hooks=\"true\" install ok")
        # File should be byte-equal (no rewrite)
        with open(cfg4, "rb") as f:
            raw = f.read().decode("utf-8")
        assert_true('hooks = "true"' in raw,
                    "PR-D: hooks = \"true\" preserved byte-equal")
        # Verify
        r = run([
            PYTHON, str(HOOKS / "verify_install.py"),
            "--runtime", "codex",
            "--codex-hooks", str(hooks4),
            "--codex-config", str(cfg4),
            "--json",
        ])
        j = json.loads(r.stdout)
        rc = j.get("runtime_checks", {}).get("codex", {})
        assert_eq(rc.get("installed"), True,
                  "PR-D: verify_install recognizes hooks = \"true\" as enabled")

        # T-PR-D-5: corrupt config.toml → invalid_runtime_config, no backup.
        cfg5 = pr_d_dir / "corrupt.toml"
        cfg5.write_text("42 = not a valid TOML line at all", encoding="utf-8")
        hooks5 = pr_d_dir / "corrupt-hooks.json"
        hooks5.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                          encoding="utf-8")
        baks_before = list(pr_d_dir.glob("corrupt.toml.bak-*"))
        r = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(hooks5),
            "--config-file", str(cfg5),
            "--no-strict-verify",
        ])
        assert_eq(r.returncode, 2,
                  "PR-D: corrupt config.toml returns 2")
        try:
            j_err = json.loads(r.stderr.strip().splitlines()[-1])
        except Exception:
            j_err = {}
        assert_eq(j_err.get("error"), "invalid_runtime_config",
                  "PR-D: corrupt config.toml reports invalid_runtime_config")
        baks_after = list(pr_d_dir.glob("corrupt.toml.bak-*"))
        assert_eq(len(baks_after), len(baks_before),
                  "PR-D: corrupt config.toml creates no backup (parse-before-backup)")

        # T-PR-D-6: collision only when write is needed.
        cfg6 = pr_d_dir / "collision.toml"
        cfg6.write_text("[features]\nhooks = false\n", encoding="utf-8")
        hooks6 = pr_d_dir / "collision-hooks.json"
        hooks6.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}),
                          encoding="utf-8")
        r1 = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(hooks6),
            "--config-file", str(cfg6),
            "--no-strict-verify",
        ])
        assert_eq(r1.returncode, 0, "PR-D collision: first install ok")
        # Second install (hooks=false again → write needed → collision)
        r2 = run([
            PYTHON, str(HOOKS / "install_codex_hook.py"),
            "--hooks-file", str(hooks6),
            "--config-file", str(cfg6),
            "--no-strict-verify",
        ])
        assert_eq(r2.returncode, 2, "PR-D collision: second install returns 2")
        try:
            j2 = json.loads(r2.stderr.strip().splitlines()[-1])
        except Exception:
            j2 = {}
        assert_eq(j2.get("error"), "backup_collision",
                  "PR-D collision: error=backup_collision")

        print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()