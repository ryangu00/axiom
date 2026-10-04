# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic
versioning once it reaches 1.0.

## [0.1.0] - unreleased

First public release. Verification-and-governance hooks for long-running coding
agents in Claude Code.

### Added
- `stuck-search` ignores user-interrupted failure events without
  changing failure clusters.
- Configurable polling exemptions default to loop constructs only;
  a leading `sleep N &&` exemption is opt-in and can hide real failures.
- Each enforce-mode advice injection writes an `advice_injected`
  ledger event, displayed separately in the report without changing recent
  observe incidents or calibration counts.
- A configurable 10-minute cooldown per cluster suppresses repeated
  enforce-mode advice; observe-mode findings remain unsuppressed.
- Configurable successful search/fetch tracking records one
  `search_after_trigger` event within 30 minutes of the latest advice in a
  session, with a lag and a separate report count.
- Temporary shell redirection, `tee`, and `sqlite3` targets receive
  advisories only, using the shared temp-root resolver and persistent-name
  patterns; shell commands are never denied by this rule.
- The host-managed per-user temporary directory is exempt by default
  on POSIX, with additional configurable directories and resolved directory
  containment checks instead of substring matching.
- Claim identity via `claim_id`, with dual-read support for legacy claims.
- Typed config loading with observable invalid and unreadable degradation.
- Observable claim-lock degradation in the ledger and `/axiom:report`.
- Standard `tests/` layout, CLI import-failure exit 2, and a hard mypy gate.
- Prior-art survey and independent-review rubric documentation.
- **write-verify** — completion claims checked against declared evidence
  predicates (file exists/contains/changed, fresh command runs); malformed
  predicates count as failed evidence; cross-session compare-and-clear on the
  active claim.
- **stuck-search** — repeated-failure fingerprinting. At threshold, enforce
  mode injects stop-and-search guidance and observe mode (the default) only
  logs. It never blocks, in either mode.
- **schema-guard** — detects persistent state written to temp paths
  (Write/Edit surface); observe mode records findings and enforce mode denies
  matching writes.
- **Privacy gate: commit-metadata scan.** `--scan-all` now covers all reachable
  commit metadata in addition to tracked file content, and `--scan-history`
  runs the metadata scan alone. Checks author/committer addresses against a
  configurable allowlist and commit messages against configurable
  attribution-trailer patterns; every finding names the ref that reaches it.
  Closes the gap that let an attribution trailer reach published history. Covers annotated
  tags as well as commits: `git tag -a` publishes a tagger and a message that
  walking commits never reaches. `--scan-blobs` adds the content of
  every reachable object, so a secret committed and later deleted is found
  where a tracked-file scan reports clean; `--scan-all` runs all three.
- **`cmd_succeeds` guard paths.** An optional `guard_paths` list on the
  predicate; a command that exits 0 while a guarded path changed, was deleted,
  appeared, or was never baselined is not a clean pass, and the evidence says
  which path and how. Closes the "tests pass because the tests changed" shape
  of a satisfied-but-dishonest claim, by hash comparison and with no model in
  the verification path.
- **preflight** — pre-mortem guidance on recognized irreversible commands;
  observe mode records findings and enforce mode injects guidance. It never
  blocks in either mode.
- **Observe mode** (the default) records findings without blocking. In
  enforce mode, `write-verify` blocks completion and `schema-guard` denies
  matching writes. `preflight` and `stuck-search` only advise and never block
  in either mode: observe logs findings, while enforce injects guidance.
- Provider layer: filesystem/git write-verifier; lessons.md, Claude Code
  memory, and an external-knowledge-base adapter for recall/persist, with
  untrusted-input quarantine.
- Commands: `/axiom:report`, `/axiom:enforce`, `/axiom:onboard`,
  `/axiom:uninstall`.
- Goal and routing templates; failure-mode taxonomy; egress-gate design note.
- **Calibration status page** (`docs/CALIBRATION.md`). What has been measured
  toward the v1.2 false-positive/false-negative commitment and what has not.
  For `write-verify`: a replay of the private predecessor's trigger logic on
  one operator's sessions (70 labeled cases, 4 positives of which 3 are
  synthetic) and one batch of 50 labeled live firings. For `stuck-search`: a
  replay set exists and none of its figures are published. The commitment
  stays open; the README says so.
- **False-green catalogue for the history scan**
  (`docs/HISTORY-SCAN-FALSE-GREENS.md`). Fifteen classes of "the scan reported
  clean on content that would still have been published", each with its regression test
  and, for nine of them, a minimal reproduction in ordinary git.
- **Known limitation: `stuck-search` guidance.** In the predecessor's data, 1
  of 8 firings with a transcript was followed by a search or fetch within the
  nudged agent's next 15 tool calls, and that one cannot be credited to the
  nudge. Documented with what that does and does not support, including a
  first reading of 0 of 8 that had looked only at top-level transcripts.
- **Egress-gate design note: field results.** A fifth decision (the canary
  asserts the path it expects to take, not a boolean), three measured results
  from a second cross-family review, and aggregate running numbers with the
  false-positive handling history.
- Pre-commit privacy gate with a `--scan-all` release mode; CI matrix
  (Python 3.10-3.12, Linux + macOS + Windows).
- Windows support: `msvcrt.locking` state locks, `CommandLineToArgvW` command
  parsing, UTF-8 subprocess decoding, Windows CI runner.
- `enforce --by NAME` records who asserted a mode change; without it the
  ledger says `unattested` instead of crediting a human by default.

### Changed
- Docs no longer say `stuck-search` "forces" a stop; the code injects guidance
  and cannot force anything.
- Egress-gate design note: the override decision no longer claims "not an env
  var". The reference implementation keeps a reusable host-environment
  override alongside the single-use token.
- PRIOR-ART: the predecessor's write-verification corpus is one runtime's
  transcripts, not several; several thousand files were mined, 70 cases
  labeled.
- README and ADAPTERS no longer say the shipped hooks run in production. The
  private predecessor does; the shipped hooks have no live data yet.
- CALIBRATION dates its corpus: one snapshot mined 2026-07-10 covering about
  six and a half weeks of transcripts.
- README, FAQ, KNOWN-LIMITATIONS and PRIOR-ART: the claimcheck extraction
  method is recorded as declined in ROADMAP, not "credited on the v1.2
  roadmap".
- FAQ: the v1.2 rates commitment is marked not yet met, with a link to
  CALIBRATION; ROADMAP carries the item.
- KNOWN-LIMITATIONS: two sentences about the history scan caught up with the
  code (binary blobs are detected by a NUL near the start; tag objects are
  found under any ref, not only `refs/tags/`).
- The escape hatch quoted in every block reason now matches the CLI's real
  argument order (`/axiom:enforce <rule> off`); the adapter CLI no longer
  forwards the Claude-only slash command to other hosts.
- `read_ledger` splits on `\n` only; records whose text carried U+2028,
  U+2029 or NEL were previously dropped.
- `schema-guard` and `preflight` share one temp-root resolver that includes
  `TEMP`/`TMP` and `tempfile.gettempdir()` for platform temp-directory detection.
- The temp-root resolver excludes `tempfile.gettempdir()` when it resolves
  to the current working directory or an ancestor, so Python's cwd fallback
  cannot mark an entire project temporary. Configured roots and explicit
  environment variables retain their existing behavior.
- On macOS, the temp-root resolver also reads the per-user temporary directory
  through libc `confstr(_CS_DARWIN_USER_TEMP_DIR)` when `TMPDIR` is absent.
  Discovery uses stdlib `ctypes`, no subprocess, and preserves the other roots
  on error; a macOS test checks the result against `getconf DARWIN_USER_TEMP_DIR`.
- The Windows worked example restores the local-pass/CI-failure history and
  generates CLI request JSON with Python so cwd quotes and backslashes are
  escaped; Windows commands use `python`.
- Mode descriptions in the enforce command, security policy, and changelog
  distinguish the two blocking rules from the two advisory-only rules.
- The worked goal file now lives at
  `docs/examples/windows-support.example.md`, a name the goal-file glob does
  not match, so opening this repo never registers a claim that runs the full
  test suite at every Stop. A test keeps the repository root free of goal
  files.
- Consolidated runtime and provider predicates in one canonical evaluator; the
  provider now requires canonical `cmd` and drops `command`/`argv` aliases
  (pre-publication breaking change).
