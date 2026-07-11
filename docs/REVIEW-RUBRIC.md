# Review rubric (frozen)

**Version 1.0 — 2026-07-10.** Changing this rubric requires editing this file
in its own commit with a rationale; scores produced under an older version say
so. A rubric you can quietly rewrite after scoring is not a rubric.

## Meta-rules (violating any of these voids the score)

1. **Rubric before scores.** The reviewer states the rubric (this file, or an
   explicit alternative) *before* producing numbers — never anchors first and
   rationalizes after.
2. **Leadership requires research.** The market-leadership dimension may not be
   scored from training-data priors. The review must disclose: scan date,
   queries used, and sources read. A "there must be something better" prior
   with no named competitor is not evidence — it was exactly the flaw that
   produced an indefensible 6.0 in our own first review round.
3. **Evidence per score.** Every dimension score cites concrete evidence
   (file:line, command output, or a named external artifact with access date).
4. **Counter-argument per score.** Every dimension includes the strongest
   argument *against* the reviewer's own number.
5. **Method disclosure.** The review states plainly whether external research
   was performed, and what was scored from priors.
6. **Missing-evidence cap.** Where the required evidence class is absent, the
   dimension is capped (see per-dimension caps), not estimated.

## Dimensions and anchors (0–10)

### 1. Market leadership

Requires a fresh (≤30 days) targeted competitive scan.

- **3** — a named, verified competitor does the same whole job better.
- **5** — parity: named peers cover the same ground with comparable depth.
- **7** — no verified overall superior in the scan, but close neighbors
  overlap on major components (name them and the overlap).
- **9** — no meaningful neighbor found *and* independent adoption evidence.
- **Cap:** no compliant scan → max **5**, stated as unresearched.

### 2. Code cleanliness

- **3** — lint/format/type/test gates fail or don't exist.
- **5** — gates pass; known dead code, duplicated load-bearing logic, or
  misleading structure remain.
- **7** — gates pass; no dead code; single implementations of load-bearing
  logic; tests assert behavior (not counts).
- **9** — additionally: typed boundaries for cross-module data, standard
  discoverable test layout, process-boundary contracts tested (CLI exit
  codes), and every deliberate shortcut documented where it lives.
- **Cap:** no type checking as a hard gate → max **7**.

### 3. Engineering sophistication

Scored against the project's *own* hardest problem (for Axiom: runtime claim
re-verification + cross-session claim lifecycle), not software in general.

- **3** — the hard problem is described, not mechanized.
- **5** — mechanisms exist but assume a single process/session; failure modes
  undocumented.
- **7** — concurrency/cross-session mechanisms with locked semantics and
  regression tests; degradation documented.
- **9** — real multi-process proofs (not simulated interleavings), observable
  degradation (not silent), and published error rates from a real corpus.
- **Cap:** flagship mechanism without regression tests → max **4**.

### 4. Product philosophy

Five components, 0–2 points each (sum = dimension score):

1. **Epistemic boundaries** — testimony vs artifact vs independent evidence
   kept distinct; predicates claim only what they check.
2. **Power & failure semantics** — observe/enforce, fail-open/fail-closed,
   escape hatches: explicit, consistent, never conflated.
3. **Human-machine governance** — what changes autonomously vs what needs a
   human is precise, and the human step costs little enough to be real.
4. **Falsifiability & honest limits** — limitations enumerated, shipped vs
   roadmap separated, claims testable.
5. **Principle-to-mechanism consistency** — the philosophy is implemented in
   code/ledgers/tests, not only prose.

- **Penalty:** any instance of roadmap sold as shipped → −2 on this dimension.

## Procedure

- Two independent reviewers minimum, cross-family preferred; both raw reviews
  are preserved, and the reconciliation records **whose** number was adopted
  and why (provenance — never average away a disagreement silently).
- Disagreement > 1.5 on a dimension → adjudicate with evidence or bring a
  third reviewer; the adjudication note is part of the record.

## Scoreboard archive (snapshots, not rubric outputs)

| Date | Round | Leadership | Code | Engineering | Philosophy | Provenance |
|---|---|---|---|---|---|---|
| 2026-07-10 | 1 (voided) | 6.0 / 7.0 | 7.0 / 6.0 | 7.0 / 7.0 | 6.5 / 9.0 | OpenAI-family / DeepSeek-family; **voided under meta-rules 1–2** (no rubric declared, no external research) — archived as the cautionary example |
| 2026-07-10 | 2 (evidence-based) | **7.0** | **7.5** | **7.5** | **8.0** | OpenAI-family reviewer proposed 8.0 leadership after a compliant scan (named zero verified superiors); orchestrator adjudicated down to 7.0 on groundtruth's proximity (named neighbor, major-component overlap). Other three adopted from reviewer round 2. |
| 2026-07-10 | 3 (post-v1.1 release gate) | **7.0** | **8.0** | **8.0** | **8.0** | OpenAI-family reviewer, rubric-compliant (fresh scan with disclosed queries; leadership 7 anchor: named neighbors, no verified superior). Scored after the v1.1 refactor (canonical evaluator, claim_id lifecycle, typed config, observable degradation, tests/ layout, mypy hard gate). Same review filed two release findings — a broken exact-gate spec (orchestrator error, gate redefined) and a README uninstall overclaim (fixed in the same commit as this row). |

| 2026-07-11 | 4 (two-family MOA re-review) | 7.5 / 7.0 | 8.5 / 8.0 | 8.5 / 8.0 | **9.0 / 9.0** | OpenAI-family (live repo, own fresh scan, scores formed before reading this table, deviations from round 3 explained per dimension) / DeepSeek-family (bundle with this table withheld for anti-anchoring; leadership relies on the documented PRIOR-ART scan, disclosed). Round-1's widest split (philosophy 6.5 vs 9.0) converged at 9.0/9.0 after the v1.1 narrative-to-evidence fixes. Both reviewers: install, observe-mode first. |

All rounds' full texts are preserved off-repo by the operator; this table is
the durable summary.
