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
