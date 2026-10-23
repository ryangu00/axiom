# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic
versioning once it reaches 1.0.

## [0.1.0] - unreleased

First public release. Verification-and-governance hooks for long-running coding
agents in Claude Code.

### Added
- Enforced decisions are recorded: `write-verify` and `schema-guard` write a
  `blocked` event before they block or deny, `preflight` writes `asked`
  before it puts its questions to the user, `stuck-search` writes
  `advice_injected`, and the adapter CLI's
  `verify` leaves the same `unverified_completion` / `verified` /
  `would_have_blocked` / `blocked` records the Stop hook leaves. Before this
  the ledger went blind the moment a rule was enforced. `/axiom:report`
  shows the enforced counts next to the observed ones and marks a blocked
  incident `| blocked` so it is never read as an observe finding.
- `finding_id` on claim, guard and `stuck-search` records, so the observe
  record, the block (or the injected advice and the search that answered it)
  and the eventual `verified` of one finding can be joined without parsing
  claim bodies (CONTRACTS §6 lists every ledger event and every derivation).
  `stuck-search`'s `advice_injected` record is now best-effort like the other
  enforce-mode records: a full disk no longer costs the agent the advice.
- A corrupt active-claim slot is `claim_unreadable`, never "no claim": the
  Stop hook records it and blocks once in enforce mode (honoring the re-entry
  cap, and without telling the agent to delete the slot — that would be the
  bypass), the CLI reports it as a `failed` verify with a `claim_slot`
  evidence item so adapter hosts block once too instead of failing open, and
  the next registration quarantines the bytes as
  `active.json.corrupt-<stamp>` instead of overwriting them. Quarantined
  copies are listed for uninstall.
- Fail-open is visible to the host: every hook that raises — the SessionStart
  health check included — now returns a `systemMessage` on stdout (no
  decision, so the host still proceeds) in addition to stderr and the ledger
  `error` record; the adapter CLI's internal errors leave the same ledger
  record; the report shows the count and latest episode under "Fail-open".
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
- **preflight** — recognized irreversible commands. Observe mode records one
  finding per detected class; enforce mode asks the user to confirm, with the
  recovery and scope questions in the permission prompt, before the command
  runs. It never denies on its own; in a non-interactive `-p` run nobody can
  answer the prompt, so the host denies the command and the agent reads the
  questions.
- **Observe mode** (the default) records findings without blocking. In
  enforce mode, `write-verify` blocks completion, `schema-guard` denies
  matching writes, and `preflight` asks the user before an irreversible
  command runs. `stuck-search` only advises and never blocks in either mode:
  observe logs findings, while enforce injects guidance.
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
- `preflight` enforce mode asks instead of advising. Context a PreToolUse
  hook adds reaches the agent next to the tool result, after the command has
  run, so "answer all three before proceeding" arrived too late to act on.
  The ask has no cooldown, since a cooldown would let the next irreversible
  command through unasked: `rules.preflight.cooldown_minutes` is no longer
  read and `preflight.json` is no longer written (uninstall still removes
  one left by an earlier version). `/axiom:enforce preflight on` says so.
- `preflight` records every detected class every time: one
  `would_have_blocked` (observe) or `asked` (enforce) per class. The cooldown
  used to drop observe records as well, and only a command's first class
  was recorded.
- `preflight` reads the shell shape instead of the raw text, through a small
  lexer shared with `stuck-search` (quotes, escapes, comments, line
  continuations, redirections, here-documents, substitutions): commands
  inside `if`/`for`/`while`/`case` bodies; wrappers such as `sudo`, `env`,
  `timeout`, `stdbuf`, `ionice`, `flock`, `watch` and `xargs`, and absolute
  paths (`/bin/rm`); `bash -c`, `su -c`, `eval`, `ssh host cmd`,
  `docker`/`kubectl exec`, `git submodule foreach`, `$(…)`, backticks,
  `<(…)`, `find -exec`, and a here-document that a shell in its pipeline
  reads (also through `ssh`, `docker`/`kubectl exec`, subshells and brace
  groups) or that its pipeline writes to a file run later in the same
  command (`sh x.sh`, `./x.sh`, `source`, `bash < x.sh`). The
  commit idiom `git commit -m "$(cat <<'EOF' …)"` lexes like any other
  command, and writing a script without running it is not running it. The
  `git` classes and `dd`/`mkfs`/`shred`/`truncate` also match behind an
  unknown wrapper; `rm` only in command position. `git push
  --force-with-lease` / `--force-if-includes` and `--dry-run`/`-n` pushes
  and cleans are no longer flagged; `git push origin +ref` is. `shred` and
  `truncate` to size 0 are `disk_overwrite`. A quoted argument is no longer
  read as a command (`echo "git reset --hard"`, a commit message), except
  for `drop_database`, which still matches SQL anywhere in the text. Text
  that does not lex falls back to the earlier raw-text matching.
- `stuck-search` matches a failed command against a cluster's first and
  latest command instead of the union of every command it absorbed. The
  union drifted: five `npm test <x>` failures split into clusters of 3 and 2.
- `stuck-search` counts a run that exits 0 but whose output shows a
  test-runner failure (`pytest | tail`), and a success takes one failure off
  its cluster instead of clearing it; exit 0 counts as success only for a
  single command or an `&&` chain, and a Bash call run in the background
  changes nothing. Commands that only print existing text (file tools,
  `git`, `gh`, `curl`, container and pod logs, compound-command words) are
  not scanned. Both changes can keep a cluster at threshold longer, so
  enforce mode can advise more often than before. Its records
  carry `masked_failure` and `distinct_errors` (distinct error signatures in
  the cluster), and the advice names the event that fired it.
- `stuck-search` state holds fingerprints, not text: digests of tokens and
  error signatures replace the raw tokens, `last_command` and `last_error`.
  Clusters written by earlier versions are dropped on the next write, and
  their counts restart.
- A broken config is recorded once per session for each broken state of the
  file, not once per hook invocation (every tool call while it stayed
  broken). The marker, `config-degraded.json`, holds fingerprints and session
  ids and is listed for uninstall. A config file that is not UTF-8, nests too
  deeply or holds an integer over 4,300 digits is now `invalid` (recorded)
  instead of an exception that failed the hook open.
- The false-positive/false-negative rates commitment no longer names a
  release (was v1.2). It is conditional on outside usage that meets the
  criteria in CALIBRATION.md; firings from operators other than the author are
  the necessary first step, not the whole condition. "Unbounded reads" in KNOWN-LIMITATIONS no
  longer says v1.2 either, matching ROADMAP, which ships it on first report.
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
