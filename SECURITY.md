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

- Advisory rules (`schema-guard`, `preflight`) warn; they do not block. Gaps in
  their coverage are hint-accuracy issues, tracked in known limitations.
