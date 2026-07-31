# Host adapters

Axiom's evaluator and claim lifecycle are host-agnostic Python with no
imports from any agent runtime. The Claude Code hooks are the **first
adapter, not the product**. This page freezes the adapter contract so any
agent runtime — including ones we have never heard of — can wire Axiom in,
and states plainly what ships versus what is roadmap.

**All four target runtimes ship today: Claude Code, Codex CLI, hermes-agent,
and OpenClaw.** Each adapter's control path was verified against the host's real
consumption seam, not just its documented hook shape, with production state left
untouched:

- **Codex** — native Stop hook; register on `SessionStart`, block on a failing
  `Stop` verify, re-entry cap, silent pass with claim clear, verified under live
  `codex exec` in an isolated home.
- **hermes** — `pre_verify` loop; control-consumption proven in-process against
  hermes' own `get_pre_verify_continue_message`.
- **OpenClaw** — `before_agent_finalize`; the adapter's `{action:"revise",
  retry}` proven consumed in-process by OpenClaw's own
  `runAgentHarnessBeforeAgentFinalizeHook`, with the re-entry cap enforced by
  the runtime's retry budget.

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
| [hermes-agent](https://github.com/NousResearch/hermes-agent) | native `pre_verify` verify-loop hook + `on_session_start`; adapter at [`adapters/hermes/`](../adapters/hermes/) | ✅ **ships** — thin plugin over the shared `axiom-adapter-cli/v1`; control-consumption proven on hermes' real seam |
| [Codex CLI](https://github.com/openai/codex) (>= 0.144.1) | native lifecycle hooks (`SessionStart`, `Stop`) with the same decision protocol as Claude Code; adapter at [`adapters/codex/`](../adapters/codex/) | ✅ **ships** — thin shim over the shared `axiom-adapter-cli/v1`; e2e-verified under live `codex exec` |
| [OpenClaw](https://github.com/openclaw/openclaw) | native `before_agent_finalize` (revise) + `session_start`; adapter at [`adapters/openclaw/`](../adapters/openclaw/) | ✅ **ships** — thin plugin over the shared `axiom-adapter-cli/v1`; revise/retry consumption proven on OpenClaw's real seam |

## Verified host versions

Each adapter was verified against the host version below. Adapters **fail open
on any error**, so a newer host that changes a payload field degrades to
"agent proceeds, error logged" — never a wedged host — rather than hard-failing
to install. Re-verify (and bump this table) when adopting a new host major.

**Read this table as author-run evidence, not as something this checkout
proves.** Verifying a consumption seam requires the host installed and running,
so those runs happened on the author's machines against real installs; the
probe transcripts are not in this repo and CI does not reproduce them. What CI
*does* prove is the layer below: the adapter contract tests
(`tests/test_codex_adapter.py`, `tests/test_hermes_adapter.py`,
`adapters/openclaw/lib.test.js`) drive each shim against the real CLI and
evaluator and pin the exact request/response shapes. If you can run one of
these hosts, the honest check is to run the adapter yourself and open an issue
if the seam has moved.

| Host | Verified version | Consumption seam exercised | Evidence |
|---|---|---|---|
| Claude Code | 2.1.x | native `Stop` hook (shipped since v1) | in-repo seam tests (`tests/test_hook_seam.py`) |
| Codex CLI | 0.144.1 | live `codex exec` (real-host e2e) | author-run |
| hermes-agent | 0.18.0 (vendor v2026.7.1) | `get_pre_verify_continue_message` (in-process, real fn) | author-run |
| OpenClaw | 2026.6.11 | `runAgentHarnessBeforeAgentFinalizeHook` (in-process, real fn) | author-run |

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
