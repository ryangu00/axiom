# Axiom

**Trust nothing unaudited — including yourself.**

Your agent says *"done."* Axiom checks.

Axiom is a verification-and-governance layer for long-running coding agents in
[Claude Code](https://code.claude.com/docs/en/hooks). Every claim an agent
makes — that it finished, that the plan is sound, that a lesson is worth
keeping — is treated as **unverified until it survives an audit against
evidence.** That is not a feature. It is the single principle everything else
in this plugin falls out of.

It installs in one command, runs in **observe-only mode for its first week**
(recording, never blocking), and uninstalls to an enumerable set of files. You
never have to trust the README — you look at what it *would have caught* in
your own loops, then decide.

---

## The one idea

A coding agent's output is testimony, not proof. In a single interactive
session you are the check — you read what came back. In a
[loop](https://addyosmani.com/blog/loop-engineering/) — where the agent runs
unattended, prompted by a schedule or a `/goal` condition instead of by you —
nobody is reading. The agent's "done" becomes the premise of the next step,
and a false one compounds silently.

Claude Code's own `/goal` closes a loop on a stopping condition, and a small
model judges whether you're done — but [that judge reads the conversation, not
the filesystem](https://code.claude.com/docs/en/hooks): *done is a claim, not
a proof.* Axiom is the missing half: it checks the claim against the
environment.

## What it does, across the loop

Axiom is not an orchestrator — Claude Code already ships the loop primitives
(scheduling, worktrees, skills, subagents, `/goal`). Axiom inserts one act of
verification at each station of that loop:

| Loop station | The unverified claim | Axiom's check |
|---|---|---|
| **Plan** (forge a goal) | "this plan is right" | a first-principles skeptic lane + pre-mortem, reconciled against experience before the plan is accepted |
| **Execute** | "I finished it" | `write-verify` — completion is checked against **declared evidence predicates** (files, git, fresh command runs), never inferred from a dirty working tree |
| | "one more fix will work" (x8) | `stuck-search` — failures are fingerprinted across attempts; at threshold, a forced stop-and-search-externally |
| **Review** | "the code is fine" (said by the coder) | the producer never signs off on itself; risk-rated work gets an independent, cross-family reviewer |
| **Evolve** | "the machine learned a better rule" | routing/threshold changes are proposed to a ledger a **human approves** — never written by an unattended loop |
| **Remember** | "this recalled memory is current & safe" | every lesson carries a timestamp + source and an *unverified-memory* prefix; instruction-shaped imports are quarantined |
| **Record** | "we'll remember why we did this" | closeout leaves a worklog + decision record; not left to the context window |

You do not have to buy all of it. v1 ships the **Execute** and **Record**
mechanics as hooks; the rest is documented and staged. (See
[Capability tiers](#capability-tiers).)

## Disciplined self-evolution

The reason "self-improving agents" tend to rot is that they rewrite their own
rules unattended — one bad generalization poisons every later decision.
Frameworks that do this at scale exist and are popular; that does not make it
safe.

Axiom's stance: **paths are free, goals are human-locked, rules are
human-approved.** The agent may change *how* it reaches a fixed goal
(that's optimization, and every change leaves a changelog line). It may not
silently change *what* the goal is (that's a collapsed premise — it escalates
to you), nor rewrite its own routing/thresholds (that goes through a proposal
you approve). Self-evolution, with a discipline on it.

## Human-in-the-loop, concretely

"Human approval" is worthless as an adjective. Here is where the human
actually is:

- **After-the-fact, no interruption:** path-level decisions aren't blocked;
  they're logged. `git diff` your goal files — that *is* the audit trail.
- **Before-the-fact, batched:** rule changes land in a proposals file that
  does nothing until you run `/axiom:enforce` and approve them, one by one.
- **Before-the-fact, hard stop:** irreversible or exfiltrating actions get a
  one-line intercept telling you exactly how to allow it (a single-use token
  you type). ~10 seconds.

Two rules keep this honest: approval must cost near-zero (read one line, type
one command) or people route around it; and **the human's decision is itself
logged** — approvals, denials, and reasons are `grep`-able. Honesty here is
not a promise, it's an output of the ledger.

## Zero-risk trial

```
/plugin marketplace add <owner>/axiom
/plugin install axiom@axiom
```

On install, every hook is in **observe mode**: it records what it *would*
have done and blocks nothing. After a few days:

```
/axiom:report
→ In the last 7 days, Axiom would have caught:
    3 false-completion claims  (agent said done; files unchanged)
    2 stuck retry loops        (same failure ≥3×; a known fix existed)
    1 persistent write to /tmp
  → enable enforcement:  /axiom:enforce write-verify
```

If it caught nothing, that's an honest result — your loops are clean. Remove
it and move on: `/axiom:uninstall` enumerates and deletes the files this
plugin manages. (The host keeps plugin *cache* copies for a grace period;
Claude Code manages those, not us — we don't claim to erase what we don't
control.)

## Born from incidents

None of these thresholds are guesses. Each hook exists because a specific
failure cost real time in months of daily long-horizon agent operation:

- **write-verify** ← agents reporting "done" on work that never touched disk,
  including a memory system that reported healthy for 13 days while silently
  not writing.
- **stuck-search** ← a full night burned retrying an environment failure that
  had a verbatim fix sitting in a forum thread.
- **preflight** ← an unmemory-verified destructive command that took a machine
  down.

The full taxonomy is in [docs/failure-modes.md](docs/failure-modes.md). What
open-source ships in this space is either a methodology essay or a feature
list; this is the residue of post-mortems.

## Capability tiers

Axiom unlocks with your setup — nothing is forced on:

- **L0 (zero-config):** the verification hooks, observe-mode by default. Works
  the moment you install.
- **L1 (cost-routing):** if you run a multi-model setup (e.g.
  [CCR](https://github.com/musistudio/claude-code-router)) or an external
  agent CLI, the routing table + dispatch discipline activate. *(schema
  reserved in v1; implementation staged.)*
- **L2 (evolve):** once the ledger has enough samples, threshold
  self-calibration proposes changes — that you approve. *(staged.)*

## Memory: bring your own

Axiom's memory provider defaults to Claude Code's own on-disk memory. Outgrow
flat files and you can point the provider at a real knowledge base — e.g.
[GBrain](https://github.com/garrytan/gbrain), an open-source brain layer — for
graph-backed recall and write-verification receipts. The provider interface is
not theoretical: the author runs these hooks in production against exactly such
a self-hosted store.

## Why not just `/goal`?

`/goal` tells you *when to stop*. Axiom tells you *what happened*. `/goal`'s
completion judge reads the conversation and resets its baselines on
`--resume`; Axiom's goal files are on-disk, structured (`done_criteria` /
`route` / `changelog`), survive session loss, and are diffed against real
evidence at closeout. They stack: run `/goal` inside a task; let the goal file
own *what done means*. Single-session, throwaway work? `/goal` alone is enough
— forging a goal file for it is overhead, and Axiom says so.

## Honest limits

- The evidence-predicate model verifies *what you declare* a machine can
  check. It does not divine correctness you didn't specify. False-positive and
  false-negative boundaries are documented, not hidden.
- Thresholds are calibrated on one operator's workload (months of daily use,
  four execution lanes — varied, but n=1). Your mileage will differ; that's
  what observe mode is for.
- Claude Code's own roadmap is moving into this territory (hooks, `/goal`,
  `/code-review`). Axiom is designed to **ride that roadmap, not race it** —
  the hooks sit on the official hook API, the goal files sit above `/goal`.
  Where the platform absorbs a piece, you lose nothing you were depending on.

## Known limitations

Audited boundaries, v1.1 hardening targets, and the n=1 calibration caveat are enumerated in [docs/KNOWN-LIMITATIONS.md](docs/KNOWN-LIMITATIONS.md) — written in the same spirit as everything else here: claim only what an audit backs.

## License

Apache-2.0. See [LICENSE](LICENSE).
