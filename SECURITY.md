# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via the repository's
**Security → Report a vulnerability** (GitHub private advisory). Do not open a
public issue for security reports.

Include: affected file(s), a minimal reproduction, and the impact you observed.
You'll get an acknowledgement within a few days.

## Scope and design notes

Axiom runs as Claude Code hooks with your user privileges. Relevant design:

- **Hooks fail open on their own errors** so a broken guard cannot wedge your
  session — meaning a disabled/misconfigured hook provides *no* protection.
  The SessionStart health check warns loudly when a hook is unloadable.
- **Recalled memory is untrusted input.** Imported lessons are prefixed
  `[unverified memory]` and instruction-shaped text is best-effort quarantined
  — a heuristic, not a seal (see `docs/KNOWN-LIMITATIONS.md`).
- The **egress-gate design** (fail-closed, criteria single-sourced, single-use
  logged override) is documented in `docs/privacy-egress-design.md`; the
  shippable provider-based version is on the v2 roadmap.

## What is not a vulnerability

- Observe mode (the default) records findings without blocking. In enforce
  mode, `write-verify` blocks completion, `schema-guard` denies matching
  writes, and `preflight` asks the user to confirm a recognized irreversible
  command before it runs. `stuck-search` only advises and never blocks in
  either mode: observe logs findings, while enforce injects guidance.
  Coverage gaps in `preflight` and `stuck-search` are heuristic-accuracy
  issues, tracked in known limitations: a shape `preflight` does not
  recognize runs without the prompt, which is why it is documented as a
  speed bump, not a sandbox.
