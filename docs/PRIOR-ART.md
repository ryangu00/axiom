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
derives from (12,614 write calls from 5,509 real sessions; golden set
expanded 20→70; precision/recall published in that system's calibration
report, 2026-07-10) and committed to shipping published false-positive/
false-negative rates for Axiom itself in v1.2.

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

### LLM-as-judge audit gates

Community toolkits list Stop-hook gates that use a *second model* to audit
the first (e.g. a Gemini-based "independent quality gate" entry in
rohitg00/awesome-claude-code-toolkit, accessed 2026-07-10; described here as
listed, not independently audited by us). Axiom deliberately takes the other
branch: deterministic evidence (stat, hash, regex, exit code) instead of a
second model's opinion — a judge model can be wrong in the same correlated
ways as the model it judges.

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
