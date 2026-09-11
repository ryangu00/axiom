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
- **stuck-search** — repeated-failure fingerprinting; forced stop-and-search at
  threshold.
- **schema-guard** — advisory interception of persistent state written to temp
  paths (Write/Edit surface).
- **Privacy gate: commit-metadata scan.** `--scan-all` now covers all reachable
  commit metadata in addition to tracked file content, and `--scan-history`
  runs the metadata scan alone. Checks author/committer addresses against a
  configurable allowlist and commit messages against configurable
  attribution-trailer patterns; every finding names the ref that reaches it.
  Closes the gap that let an attribution trailer reach published history.
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
- Pre-commit privacy gate with a `--scan-all` release mode; CI matrix
  (Python 3.10-3.12, Linux + macOS).

### Changed
- Consolidated runtime and provider predicates in one canonical evaluator; the
  provider now requires canonical `cmd` and drops `command`/`argv` aliases
  (pre-publication breaking change).
