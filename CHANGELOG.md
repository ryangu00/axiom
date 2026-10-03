# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses semantic
versioning once it reaches 1.0.

## [0.1.0] - unreleased

First public release. Verification-and-governance hooks for long-running coding
agents in Claude Code.

### Added
- Claim identity via `claim_id`, with dual-read support for legacy claims.
- Typed config loading with observable invalid and unreadable degradation.
- Observable claim-lock degradation in the ledger and `/axiom:report`.
- Standard `tests/` layout, CLI import-failure exit 2, and a hard mypy gate.
- Prior-art survey and independent-review rubric documentation.
- **write-verify** — completion claims checked against declared evidence
  predicates (file exists/contains/changed, fresh command runs); malformed
  predicates count as failed evidence; cross-session compare-and-clear on the
  active claim.
- **stuck-search** — repeated-failure fingerprinting; injects stop-and-search
  guidance at threshold. Advisory in every mode: it never blocks.
- **schema-guard** — advisory interception of persistent state written to temp
  paths (Write/Edit surface).
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
- **preflight** — pre-mortem prompt on recognized irreversible commands.
- **Observe mode** by default: hooks record, never block, until you enable
  enforcement per rule.
- Provider layer: filesystem/git write-verifier; lessons.md, Claude Code
  memory, and an external-knowledge-base adapter for recall/persist, with
  untrusted-input quarantine.
- Commands: `/axiom:report`, `/axiom:enforce`, `/axiom:onboard`,
  `/axiom:uninstall`.
- Goal and routing templates; failure-mode taxonomy; egress-gate design note.
- **Calibration status page** (`docs/CALIBRATION.md`). What has been measured
  toward the v1.2 false-positive/false-negative commitment and what has not:
  a replay of the private predecessor's trigger logic on one operator's
  sessions (70 labeled cases, 4 positives of which 3 are synthetic) and one
  batch of 50 labeled live firings. The commitment stays open; the README says
  so.
- **False-green catalogue for the history scan**
  (`docs/HISTORY-SCAN-FALSE-GREENS.md`). Fifteen classes of "the scan reported
  clean and the content was still published", each with its regression test
  and, for the six that ordinary git can produce, a minimal reproduction.
- **Known limitation: `stuck-search` guidance.** In the predecessor's data, 0
  of 8 firings with a transcript were followed by a search within the next 15
  tool calls. Documented with what that does and does not support.
- **Egress-gate design note: field results.** A fifth decision (the canary
  asserts the path it expects to take, not a boolean), three measured results
  from a second cross-family review, and aggregate running numbers with the
  false-positive handling history.
- Pre-commit privacy gate with a `--scan-all` release mode; CI matrix
  (Python 3.10-3.12, Linux + macOS).

### Changed
- Docs no longer say `stuck-search` "forces" a stop; the code injects guidance
  and cannot force anything.
- Egress-gate design note: the override decision no longer claims "not an env
  var". The reference implementation keeps a reusable host-environment
  override alongside the single-use token.
- PRIOR-ART: the predecessor's write-verification corpus is one runtime's
  transcripts, not several.
- KNOWN-LIMITATIONS: two sentences about the history scan caught up with the
  code (binary blobs are detected by a NUL near the start; tag objects are
  found under any ref, not only `refs/tags/`).
- Consolidated runtime and provider predicates in one canonical evaluator; the
  provider now requires canonical `cmd` and drops `command`/`argv` aliases
  (pre-publication breaking change).
