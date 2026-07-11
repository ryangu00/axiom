# Host adapters

Axiom's evaluator and claim lifecycle are host-agnostic Python with no
imports from any agent runtime. The Claude Code hooks are the **first
adapter, not the product**. This page freezes the adapter contract so any
agent runtime — including ones we have never heard of — can wire Axiom in,
and states plainly what ships versus what is roadmap.

**Only the Claude Code adapter ships today.** Everything else on this page
is labeled roadmap or invited, per this repo's rule that design is never
sold as installed behavior.

## The contract: three verbs

An adapter is anything that wires these three calls into a host's lifecycle.
State lives on the filesystem (claims, ledger, config under the data root),
so hosts share nothing but paths.

1. **register** — at task start, put a claim on disk: either drop a
   `*.goal.md` file with an `## acceptance` JSON block (the zero-code path —
   `register_goal_claim()` picks it up), or call
   `register_claim_if_absent(claim)` directly. Baseline snapshots happen at
   registration.
2. **verify** — at turn/task end, evaluate the active claim
   (`process_stop`-equivalent: read claim → evaluate predicates fresh →
   compare-and-clear on success). The verdict is structured
   (`passed`, per-predicate `evidence`, failure reason); the host maps it to
   whatever it can do — block the turn, inject a message, or just log.
   Fail-open on adapter errors is part of the contract.
3. **observe** *(optional)* — feed tool outcomes to the advisory rules
   (`stuck-search`, `schema-guard`, `preflight`). Skipping this loses hints,
   not the evidence chain.

The Claude Code hooks already speak this shape (stdin JSON in, decision JSON
out); the host-specific part of that adapter is payload field names and
`hooks.json` wiring — a thin shim over the same core calls.

## Named targets

| Host | Path | Status |
|---|---|---|
| [Claude Code](https://code.claude.com/docs/en/hooks) | native hooks (`Stop`, `PostToolUse`, `PreToolUse`) | ✅ **ships (v1)** |
| [hermes-agent](https://github.com/NousResearch/hermes-agent) | its documented plugin system supports custom tools and hooks; adapter = a plugin wiring the three verbs | 🗺 roadmap — high feasibility; we operate hermes-agent daily, so this adapter would be dogfooded like the first one |
| Codex CLI | as of writing (2026-07-11) Codex exposes notification-style hooks rather than a blocking lifecycle hook; candidate paths are a turn-end wrapper or post-hoc verification over session rollouts | 🗺 roadmap — research; our calibration corpus already includes Codex-lane sessions |
| [OpenClaw](https://github.com/openclaw/openclaw) | plugin system with hook declarations; the largest personal-agent community | 🗺 roadmap — we run an OpenClaw gateway ourselves, so this adapter gets dogfooded like the others; contributions welcome, the contract above is the spec |

## Why this is credible rather than a wish list

We operate all four target runtimes daily — the two coding CLIs and both
agent gateways run on our own infrastructure. The failure taxonomy behind
Axiom was distilled from incidents across four generations of agent stacks,
and the threshold calibration corpus already spans two runtimes.
Multi-runtime is where this tool came from — the adapters are the packaging
catching up to the data, and every one of them gets dogfooded before it
ships.

## Contributing an adapter

Wire the three verbs, keep fail-open semantics, do not weaken the evidence
chain (no verdicts derived from the host's own transcript — fresh evaluation
only), and ship with at least one end-to-end test per verb. Open an issue
first if the host needs contract changes; the contract file is versioned the
same way as [CONTRACTS.md](CONTRACTS.md).
