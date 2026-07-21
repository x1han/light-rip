# Changelog

All notable changes to Light RIP are recorded here. Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) but this
project is pre-1.0; minor bumps may include breaking changes until
the API stabilizes.

## [Unreleased]

### Hardening pass (stage 1, destructive-write safety)

A 4-PR hardening series plus a follow-up fix batch, focused on
making the destructive-write install path recoverable. The dedicated
installers no longer risk corrupting or destroying the user's
runtime config, and the verifier's exit codes now reflect what the
underlying state actually is.

#### `--format zcode` adapter for the reminder script (`c5a6146`)

The reminder script learned a second output envelope shape. The
default `--format harness` still emits
`hookSpecificOutput.additionalContext`, which most runtimes accept.
The new `--format zcode` emits an envelope shaped for ZCode's hook
layer. The ZCode adapter was written against ZCode's documented
`additionalContext` field but has not been end-to-end verified
against a live ZCode install; if the reminder does not appear after
installing, fall back to `--format harness`.

Also added in this commit: `LIGHT_RIP_PROBE=1` writes a probe JSON
line to stderr so the verifier can forward it; `UnicodeDecodeError`
on the reminder file now fails open instead of raising.

#### Real runtime-aware verification (`16286a9`)

`verify_install.py` now performs three layers of checks: script-side
smoke tests (`reminder.md` readable, reminder script spawnable), and
runtime-aware detection for Claude Code (`~/.claude/settings.json`),
Codex (`~/.codex/hooks.json` + `~/.codex/config.toml` features), and
ZCode (`~/.zcode/cli/config.json`). Exit code 1 reports a missing
runtime entry; exit code 2 reports a corrupt config; exit code 0
means the script-side wiring is sound and every probed runtime has
a working entry.

Dedicated installers call the verifier automatically at the end.
The `--strict-verify` flag on the installers made the verifier's
exit code propagate; under this hardening batch the default flips
to `--strict-verify` ON (use `--no-strict-verify` to opt out).

#### Shared backup + atomic write helpers (`909d732`)

New `hooks/installer_common.py` centralizes three concerns every
dedicated installer shares: date-stamped backups with abort-on-
collision semantics (`<path>.bak-YYYY-MM-DD` UTC, `BackupCollisionError`
on same-day collision), crash-safe writes (temp file + fsync +
`os.replace`), and a `load_json_safe` helper that returns a typed
error key instead of raising.

Dedicated installers were updated to use the shared helpers. Both
installers now distinguish three failure modes:
`backup_collision` (user has a choice: force or rename),
`permission_or_io_error` (filesystem problem: force-backup would not
help), and `invalid_runtime_config` (source is corrupt or non-object
— installer aborts before touching any backup).

#### Follow-up: parse-before-backup, true strict verification

This batch addresses review findings against the stage 1 series.
Five issues fixed:

  1. **`upsert_light_rip` removes ALL existing entries in the
     namespace, not just the first.** The earlier `upsert` helpers
     left behind duplicate entries from a crashed-mid-write install;
     a fresh install could then leave the user with multiple Light
     RIP entries. New behavior: filter the whole list in place.

  2. **ZCode corrupt-config branch now carries a manual-restore
     hint.** Previously the Claude and Codex branches surfaced the
     `<path>.bak-*` recovery path; the ZCode branch silently
     reported the parse error without a pointer to the most recent
     backup. Also added the same hint to all "non-object root"
     branches.

  3. **`load_json_safe` rejects non-object roots and catches
     `OSError` + `UnicodeDecodeError` + `JSONDecodeError`.** The
     installer previously called `json.loads` directly and could
     crash with a traceback on a corrupt config or raise on
     non-object roots. The helper now returns `(parsed_dict,
     error_string)` with stable error keys.

  4. **`BackupCollisionError` and `OSError` are handled as separate
     branches.** The earlier draft combined them, which would have
     reclassified a deliberate same-day collision as
     `permission_or_io_error` and made force-backup meaningless.
     They now produce distinct error keys.

  5. **`--strict-verify` is now actually strict.** Two holes were
     fixed: (a) installers previously returned 0 silently when
     `verify_install.py` was missing; under `--strict-verify` this
     is now `error=verify_missing` with exit 2. (b) `compute_exit_code`
     previously trusted `match_count` over the authoritative
     `installed` flag, so a Codex install with one matching hook
     entry but a missing `config.toml` reported `installed=False`
     yet exited 0; this is now exit 1.

  Plus three correctness fixes from the post-work review:

  6. **ZCode detector requires `hooks.enabled == true` and rejects
     non-`process` entries.** Previously any `.exe` command (e.g.
     `node.exe`) would count as a Light RIP install; the loader
     itself requires `hooks.enabled` and silently skips entries
     whose `type` is not `"process"`.

  7. **Recovery hints no longer embed shell-unsafe copy commands.**
     Paths printed in JSON output are NOT shell-escaped; users must
     quote them with their shell of choice. The hint text now says
     so explicitly.

  8. **README narrows the verifier coverage claim to Claude Code,
     Codex, and ZCode.** OpenCode and other runtimes install via
     the general prompt and are not actively verified here.

  9. **Verifier human output no longer references a nonexistent
     `install_zcode_hook.py`.** ZCode installs use the general
     prompt's worked example.

#### PR-C: PEP 394 / PEP 397 Python-launcher predicate

The ZCode detector in `verify_install.py` no longer treats every
`.exe` filename as a Python interpreter. The new predicate is a
strict regex anchored on the full filename (`cmd_path.name`, not
`.stem`) that matches the blessed names from PEP 394 (`python`,
`python2`, `python3`, `python3.12`) and PEP 397 (`py`, `py3`) with
or without a `.exe` suffix. It explicitly rejects:

  - `pythonw`, `pythonw.exe` — GUI launcher, does not allocate a
    console.
  - `python_d`, `pythonw_d` — debug builds.
  - `mypython`, `python3.12-config`, `python3.12-config.exe`,
    `pyfoo`, `python.exe.bak` — names that happen to start with
    `python` or `py` but are not interpreters.

Using `.name` (not `.stem`) is essential: on Windows,
`Path("python3.12-config.exe").stem` reduces to `python3.12`, which
would falsely match the dev tool as an interpreter. Anchoring on
the full filename closes that hole.

#### PR-D: Codex `config.toml` via `tomllib` / `tomli_w`

`install_codex_hook.py` switched from regex line surgery to a TOML
library for `~/.codex/config.toml`:

  - **`check_hooks_feature`** reads the file and decides whether a
    write is needed. Returns `(needs_write, error)` so the caller
    can react separately to "already correct" vs "corrupt".
  - **`write_hooks_feature`** reads, mutates (`features.hooks =
    True`), and writes via `tomli_w`. Caller must have already
    backed up.
  - **`ConfigTomlError`** is raised when the file is structurally
    wrong (parse error, non-dict root) or when `tomli_w` is
    missing. Mapped to `error=invalid_runtime_config` with exit 2.

The install flow now applies the same **parse-before-backup**
invariant to `config.toml` as to `hooks.json`: the source is
parsed first, and only backed up once a write is actually needed.
Re-installs where `[features] hooks` is already truthy make no
backup and no rewrite — the file stays byte-equal so its comments
and formatting are preserved.

The verifier (`verify_install.py`) was updated in parallel:

  - `_codex_feature_enabled` now accepts TOML bool `True` OR the
    case-insensitive string `"true"`, matching Codex's own
    acceptance rules. The earlier `bool(features.get("hooks"))`
    check was wrong: `bool("false")` is True (non-empty string),
    which would have reported a disabled feature as enabled.
  - The regex fallback (when neither `tomllib` nor `tomli` is
    installed) strips a single layer of surrounding quotes so it
    agrees with the TOML branch on `hooks = "true"`.

**Known regression**: when `install_codex_hook.py` actually
rewrites `config.toml`, `tomli_w.dump` does not preserve original
comments or formatting. The installer reaches this code path only
when `[features] hooks` is not already truthy; idempotent
re-installs are no-ops. If a user has hand-curated comments in
`config.toml`, take a backup before the first install on that
file (the installer creates one automatically — keep it).

**New dependency**: `tomli_w` is required for the Codex installer.
On Python 3.11+ install with `pip install tomli-w`. On Python
3.10 and earlier also install `tomli`.

#### Unified hook-based installer

The three pre-unification scripts (`install_claude_hook.py`,
`install_codex_hook.py`, plus the ZCode worked example that lived
inside `install_general_agent_hook.py`) are replaced by a single
`install_hook_based_agent.py`. Pick one runtime with `--runtime`:

  - `claude` — writes `~/.claude/settings.json` (legacy JSON hooks
    schema; `metadata.hook_namespace` based dedup).
  - `codex` — writes `~/.codex/hooks.json` AND enables
    `[features] hooks = true` in `~/.codex/config.toml` (TOML
    via `tomllib` + `tomli_w`; still requires `pip install
    tomli-w`).
  - `zcode` — writes `~/.zcode/cli/config.json` as a `process`-
    type entry under `hooks.events.UserPromptSubmit[]`. **First-
    class installer for ZCode** (previously only a worked example
    in the general installer's prompt).

**BREAKING**: `install_claude_hook.py` and `install_codex_hook.py`
are removed. Any shell scripts, CI pipelines, or documentation
that referenced those filenames must be updated to use
`install_hook_based_agent.py --runtime claude` or `--runtime codex`.

CLI surface changes:

  - New required `--runtime {claude,codex,zcode}`.
  - Path flags use sentinel `None` defaults and are filled in by
    `_validate_args` based on runtime; passing a path flag under
    the wrong runtime now aborts with
    `error=flag_not_applicable_for_runtime` and exit 2 rather
    than being silently ignored.
  - Shared error envelope helper `_emit_error` centralises the
    `print(json.dumps({...}, file=sys.stderr)); return 2` pattern
    that appeared 14+ times across the old installers; wire
    shape unchanged.
  - Three branch functions (`install_claude`, `install_codex`,
    `install_zcode`) are exposed as top-level callables so future
    tests can call them in-process without spawning subprocess.
  - The `_run_verifier` helper centralises the runtime-agnostic
    verifier invocation; each branch passes only its
    runtime-specific path flag via `verify_argv_extra`.

ZCode branch specifics:

  - Dedup predicate `_is_our_zcode_entry` matches by command
    basename + first arg basename (same criterion as
    `verify_install.check_zcode`), since ZCode's process-hook
    schema has no `metadata.hook_namespace` field.
  - New entry MUST NOT include a `matcher` field — Zod's
    `min(1)` schema rejects empty strings and silently drops
    the whole hooks block. Omit the field entirely to match all
    prompts.
  - `hooks.enabled`, `hooks.timeoutMs`, `hooks.maxOutputBytes`
    are set with `setdefault` (idempotent: do NOT clobber a
    deliberate `false`).
  - The ZCode worked example that lived inside
    `install_general_agent_hook.py:167-229` stays as
    documentation; users with ZCode now have a one-line
    installer and don't need to hand-roll the worked example.

The verifier (`verify_install.py`) is unchanged — it already
accepted `--runtime zcode --zcode-config`. The general installer
`install_general_agent_hook.py` (prompt dispatcher for any
runtime the unified installer doesn't cover) is also unchanged
structurally; its "Dedicated installers" block was updated to
mention the three `--runtime` choices.

### Docs

- `CHANGELOG.md` (this file) added.
- `README.md` extends the "Verify the install" section with the
  `--runtime` flag and strict-vs-lenient semantics, narrows the
  verifier coverage claim, and adds a "Backups and atomic writes"
  subsection explaining the abort-on-collision policy, parse-before-
  backup ordering, and the three distinct failure modes.
- `hooks/install_general_agent_hook.py` ZCode worked example now
  uses `--format zcode` and includes an explicit caveat that the
  adapter is empirically untested.
- `README.md` adds a "Dependencies" section documenting the
  `tomli_w` (and `tomli` on Python ≤ 3.10) requirement for the
  Codex installer, extends "Backups and atomic writes" with the
  parse-before-backup + idempotent-reinstall invariant on
  `config.toml`, and adds a "ZCode detector" subsection explaining
  the PEP 394/397 launcher predicate.

### Installer & doc simplification

A 8-commit refactor series focused on reducing surface area, removing
dead code, and replacing the standalone smoke harness with a
first-party `--self-test` subcommand.

#### Unified installer: argparse subcommands + `--self-test` (`c1ea776`)

The unified installer (`install_hook_based_agent.py`) restructured
into two argparse subparsers:

  - `install --runtime {claude,codex,zcode}` — the production install
    path (unchanged behaviour; old top-level `--runtime <name>` is no
    longer accepted, subcommand is required).
  - `self-test` — in-process P0-contract regression covering
    `installer_common` helpers, parse-before-backup, backup
    collision, PR-D Codex TOML byte-equal preservation, PEP 394/397
    launcher predicate (positives + negatives), ZCode detector edge
    cases, cross-runtime flag rejection, and install+verify
    round-trip for all three runtimes.

Standalone `smoke_fix_batch.py` (797 lines, 76 assertions) deleted;
its contracts are now enforced by `self-test`. Initial `--self-test`
passes 52/52.

Also in this commit: dropped the `--no-enable-feature` flag (the
Codex `[features] hooks = true` write is now unconditional), since
the only consumer was its own cross-runtime rejection test. T-UNI-3
replaced by a `--hooks-file --runtime claude` rejection case.

#### `_validate_args` compressed 91 → ~50 lines (`c096406`)

Per-runtime flag rejection paths collapsed via a `_reject(flag,
runtime, hint)` helper + module-level hint constants. Envelope wire
shape unchanged; only the source shrinks.

#### Docs: dead ZCode worked-example removed (`770008a`)

`install_general_agent_hook.py` no longer carries the ZCode install
analogy walkthrough — `--runtime zcode` does the same thing in one
command. OpenCode worked-example retained; "by analogy" structure
intact for any other hook-based runtime.

#### Docs: README install sections collapsed to a table (`493f29e`)

The three per-runtime install blocks (Claude Code / Codex / Other)
in `README.md` collapsed to a single 3-row table covering skills
dir + install command. Skills-dir conventions and `CODEX_HOME`
handling preserved in the table cells.

#### `--format zcode` deprecated; installer writes `--format harness` (`74c82e8`)

The reminder script's ZCode adapter is kept for back-compat with
installs that baked `--format zcode` into their `~/.zcode/cli/
config.json` args array, but the unified installer no longer
generates that flag. New ZCode installs write `--format harness`
(the broader envelope that ZCode also parses via its generic hook
layer). Docstring + argparse help text marked `zcode` as deprecated.

#### Docs: SKILL.md "Required Reminder Hook" trimmed (`1d777ff`)

The "Required Reminder Hook" section + per-runtime install blocks
deleted (duplicated SKILL.md lines 14-16). "Common Mistakes"
preserved — it's the anti-pattern complement to "Completion
Criteria" and has no other home.

#### Docs: SKILL.md install commands updated to subcommand form (`a2a419a`)

Follow-up to `c1ea776`: SKILL.md lines 14-16 still showed the
pre-subparser form. Fixed to `install --runtime <name>`.

#### Fix: ZCode install+verify round-trip (`9a60020`)

Closed a pre-existing bug present since the unification commit
(`0af968c`): the installer's ZCode branch wrote entries flat
(`{type, command, args, timeoutMs}`) directly under
`events.UserPromptSubmit[]`, while `verify_install.check_zcode`
expected wrapper groups `{matcher?, hooks: [<entry>]}`. The mismatch
silently produced `match_count: 0` from the verifier on every ZCode
install despite the installer reporting success — the pre-existing
smoke test missed it because T-UNI-1/2 inspected JSON directly,
never via the verifier.

Fix:

  - `install_zcode` wraps each new entry in `{"hooks": [new_entry]}`
    per the ZCode schema.
  - `_is_our_zcode_entry` accepts both wrapper and legacy flat
    shapes so re-installs clean up old flat entries instead of
    leaving dead weight.
  - Self-test gains `_self_test_install_verify_roundtrip` that
    closes the loop for all three runtimes — installer writes
    → `check_<runtime>` returns `installed=True, match_count=1`.

Self-test now passes 63/63 (52 prior + 11 round-trip assertions).