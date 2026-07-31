# Prior art & related work

Axiom's premise is that unaudited claims are worthless, so this page holds
itself to the same bar: every statement about another project cites that
project's own public documentation with an access date, absence claims are
scoped to *what the documentation shows*, and we say plainly which ideas we
adopted from whom.

## Origin boundary (what we built before we looked)

Axiom's hooks were extracted from a private verification layer built from
our own production incidents (a memory system that reported healthy for 13
days while silently not writing; an agent retrying one failing command all
night; "done" claims with no artifact on disk — see *Born from incidents* in
the README). The verification design — pre-declared machine-checkable
predicates, a registered baseline, Stop-hook re-verification through a fresh
evidence channel — predates our competitive research.

We ran a targeted competitive scan on **2026-07-10**, before first public
release. It found the neighbors below. No Axiom code is derived from them;
two of their *methods* influenced our roadmap and are credited explicitly.

## Closest neighbors

### groundtruth — vnmoorthy/groundtruth

<https://github.com/vnmoorthy/groundtruth> (accessed 2026-07-10)

*"A completion-claim gate for Claude Code. Refuses to let the agent say done
without evidence."* The closest project to Axiom's flagship, and per its
README it is **ahead of Axiom** on empirical calibration: its completion-claim
detector was tuned against 1,272 real assistant turns across 50 sessions, it
ships 153 tests, retroactive session audit with SARIF output, and a memory
gate — with no outbound network calls from the hook.

Mechanism difference (per its README as of the access date): groundtruth
detects a *natural-language* completion claim at Stop and checks that the
**same turn** contains tool-observation evidence (tests, builds, reads). Its
README does not document pre-declared predicates, a registered content
baseline, cross-session claim persistence, or concurrency-safe
compare-and-clear — which is Axiom's claim-lifecycle side of the problem.
The two designs are complementary answers to the same distrust.

**Adopted from groundtruth (with evidence):** the corpus-calibration
method — replay a detector against a large labeled corpus of real turns and
publish the confusion matrix. We applied it to the private system Axiom
derives from (a corpus of thousands of real agent sessions spanning multiple
runtimes; a several-fold golden-set expansion; precision/recall held in that
system's internal calibration report, 2026-07-10) and committed to shipping
published false-positive/false-negative rates for Axiom itself in v1.2.

### claimcheck — ojuschugh1/claimcheck

<https://github.com/ojuschugh1/claimcheck> (accessed 2026-07-10)

*"Verify whether an AI coding agent actually did what it claimed."* A Rust
CLI (crates.io) that parses a session transcript (Claude Code JSONL, Cursor,
Markdown), **extracts** every concrete claim (files created/deleted/modified,
packages installed, tests run, bug fixes, numeric claims), and checks each
against the real filesystem, git history, and lockfiles — no LLM calls,
fully local, with a truth score and per-claim PASS/FAIL/UNVERIFIABLE.

Mechanism difference (per its README as of the access date): claimcheck is
an explicitly-invoked post-hoc CLI; its README does not document running as
a host Stop gate, claim registration, baselines, or a cross-session claim
lifecycle, and its test verification defaults to runner output found in the
transcript (fresh re-run is opt-in `--retest`). Its claim *extraction* is
broader than Axiom's declared-predicate contract.

**Adopted from claimcheck (roadmap credit):** automatic claim extraction
from free text as a complement to declared predicates — lowering the "you
must write predicates" adoption barrier — is on Axiom's v1.2 candidate list
because claimcheck demonstrated it working.

### tdd-guard — nizos/tdd-guard

<https://github.com/nizos/tdd-guard> (accessed 2026-07-12) — MIT, TypeScript,
~2.3k stars at access.

*"Automated Test-Driven Development enforcement for Claude Code."* It gates
the *edit* against a methodology (no implementation without a failing test; no
code beyond current test requirements); Axiom gates the *stop* against declared
evidence. Different station, different subject, no overlap in enforcement
surface.

The instructive difference is in **how the decision is made**. tdd-guard's own
documentation is explicit: *"TDD Guard validates changes using AI. Configure
both the validation client (SDK or API) and the Claude model version"* —
validation runs through the Claude Agent SDK (or the Anthropic API for CI),
with a selectable model
([validation-model.md](https://github.com/nizos/tdd-guard/blob/main/docs/validation-model.md),
accessed 2026-07-12). The blocking *mechanism* is a deterministic hook; the
*judgment inside it* is a model's.

**Why it's here:** it marks the other side of a design split Axiom sits on.
Both projects agree a hook that blocks beats a prompt that asks. They disagree
on what decides: tdd-guard asks a model whether the work complies (which lets
it judge things no predicate can express — at the cost of an API dependency,
per-call latency, and a judgment that can differ between runs); Axiom re-runs
declared predicates with no model in the path (which is reproducible and free —
at the cost of only ever checking what you thought to declare). Neither is
strictly better; they buy different things. See also *LLM-as-judge audit gates*
below — tdd-guard is the most mature member of that family we found, and its
~2.3k stars are the clearest public evidence that hook-level enforcement of a
discipline is something developers actually want.

We take no code and no method from it.

**Correction (2026-07-12):** an earlier draft of this page described tdd-guard
as deterministic enforcement and cited it as evidence for Axiom's no-model
design. That was wrong — it came from an inference about "blocks the action"
rather than from tdd-guard's validation docs, and a cross-family review caught
it before publication. The entry above is rewritten from the primary source.

### nah — manuelschipper/nah

<https://github.com/manuelschipper/nah> (accessed 2026-07-12;
[Show HN](https://news.ycombinator.com/item?id=47343927), 2026-03-19)

*"Action-aware, deterministic permissions for coding agents."* MIT, Python,
zero required dependencies. It classifies tool calls **before execution**
against a deterministic policy (an optional LLM sits *behind* the
deterministic floor, only for unknown Bash, with deterministic re-checks
after). Its README opens: *"You shouldn't run a coding agent outside a
sandbox. Sometimes you do it anyway."*

Complementary, not competing: nah guards the *action* at PreToolUse; Axiom
audits the *claim* at Stop. A loop can run both, and they answer different
questions ("should this run?" vs "did what you said happen actually happen?").

**Adopted from nah (roadmap credit, and a bar we have not met):** nah
publishes its calibration against a **public** corpus — 101,194 Bash tool
calls extracted from the Novita Claude Code traces, of which it asked on 4.2%
and resolved 95.8% deterministically. Axiom's thresholds are calibrated on a
private corpus (n=1 operator); nah demonstrates the honest version — calibrate
on a corpus a stranger can re-run. That is the standard our v1.2
false-positive/false-negative publication is aiming at. We also borrow its
scope honesty verbatim in spirit: the sandbox is the real answer, and a
deterministic guard is for the loops you run outside one.

### LLM-as-judge audit gates

Community toolkits list Stop-hook gates that use a *second model* to audit
the first (e.g. a Gemini-based "independent quality gate" entry in
rohitg00/awesome-claude-code-toolkit, accessed 2026-07-10; described here as
listed, not independently audited by us). Axiom deliberately takes the other
branch: deterministic evidence (stat, hash, regex, exit code) instead of a
second model's opinion — a judge model can be wrong in the same correlated
ways as the model it judges.

## The large adjacent repos

The projects above are small and squarely in this niche. These three are one
to two orders of magnitude more popular and sit *next to* it — the honest
comparison matters more, not less, because a reader's first question is
reasonably "isn't this already in one of the big ones?" We audited all three
at code level (registered hook manifests and the scripts they invoke, not
their READMEs), accessed 2026-07-10.

### oh-my-claudecode — Yeachan-Heo/oh-my-claudecode

The nearest thing to Axiom's flagship in a mainstream repo, and by far the
largest project doing anything like it. It sells multi-agent orchestration
(*"Multi-agent orchestration for Claude Code. Zero learning curve."*), but it
ships a registered `Stop` hook that **does perform a narrow form of
claim-evidence verification**: `workflow-drift-guard.mjs` matches completion
wording in the agent's message, then scans changed and untracked files
(`git diff --name-only HEAD`, `git ls-files --others`) and blocks the stop if
that changed code still contains TODO/stub/skipped-test markers
(`hooks/hooks.json:172-195`; `scripts/workflow-drift-guard.mjs:53-69,202-207`).

Same station, different question. It asks *"did you leave obvious junk in the
code you touched?"* — a generic staleness scan over the diff, with no claim
registered beforehand and no baseline. Axiom asks *"did the specific thing you
said you did actually happen?"* — predicates declared before the work,
snapshotted at registration, re-run fresh. The two catch different failures:
its guard fires on a stub you left behind even when you never claimed that
file; ours fires on a test you said passed but never ran, even when the diff
looks clean. Its own separate deliverable checker is explicitly advisory and
permits the stop even when declared files are missing
(`scripts/verify-deliverables.mjs:14-22,221-229`), which is the gap Axiom's
enforce mode is for.

If you already run oh-my-claudecode, you have the drift scan and you do not
have the declared-evidence check. They compose.

### planning-with-files — OthmanAdi/planning-with-files

A persistent file-based planning skill (*filesystem as durable working memory*
— keeps `task_plan.md` / `findings.md` / `progress.md` on disk so the agent
survives `/clear`, context loss and crashes). It includes an **opt-in `Stop`
gate**, but the gate counts agent-authored phase-status strings — it hard-
blocks while a plan reads `in_progress` — rather than comparing a claim
against git or the filesystem (`scripts/check-complete.sh:74-109,164-215`).
Agent-authored status is exactly the testimony Axiom declines to trust.

It is also the honest counter-example to any multi-runtime bragging on our
part: it ships lifecycle adapters for Codex, Cursor, Gemini, Hermes and others
(`.codex/hooks.json`, `.cursor/hooks.json`, `.gemini/settings.json`). Axiom's
four-runtime coverage is not a differentiator; the declared-evidence contract
underneath it is what differs.

### agents — wshobson/agents

A cross-harness marketplace of building blocks (92 plugins / 199 agents / 162
skills / 106 commands, consumed natively by Codex, Cursor, OpenCode, Gemini
and Copilot). Runtime hooks are the exception rather than the fabric: the
public clone's only hook manifests are `protect-mcp` and
`review-agent-governance`, both limited to `PreToolUse`/`PostToolUse`
(`plugins/protect-mcp/hooks/hooks.json:1-25`;
`plugins/review-agent-governance/hooks/hooks.json:1-25`). There is no
completion-claim verifier and no `Stop`/`SessionEnd` enforcement path; its
session closeout is an agent prompt, not an enforced gate
(`plugins/operating-kit/agents/session-end.md`). Different category — listed
because "surely one of the big marketplaces covers this" deserves a checked
answer rather than a shrug.

## Method and discourse sources (not competitors)

- **Loop engineering** (Addy Osmani's essays; Anthropic's own hooks
  documentation and `/goal` primitives): the framing that agent quality
  comes from the harness around the loop, not just the model. Axiom is a
  verification/governance layer inside that discourse, not a loop framework.
- **CI / pre-commit / TDD tradition**: exists/regex/hash/exit-code checks are
  decades old. Axiom's contribution is not a new verification theory; it is
  the claim-lifecycle placement — predicates declared *before* the work,
  verified at the agent-loop boundary, with cross-session state.
- **Skepticism, from first principles**: the epistemic stance (testimony is
  not proof; verify through an independent channel) is methodological doubt
  applied to agent loops — old philosophy, deliberately applied.

## Corrections

If we have misdescribed any project above, open an issue with a pointer to
the documentation we missed; we will correct it and note the change here.
