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