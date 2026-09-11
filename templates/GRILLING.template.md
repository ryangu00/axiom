# Grilling: settle the decisions before the goal is forged

A convention, not running code. Axiom's Execute station is a hook; the Plan
station is this and the two files beside it.

## The gap this closes

A goal file's `done_criteria` is the target everything downstream gets aimed
at. If a criterion came from the agent's *inference* about what you wanted
rather than from something you actually said, the target is wrong — and a
disciplined agent will then drive at that wrong target with full commitment,
because the rest of the loop is built to stop it from second-guessing mid-run.

The usual review lanes do not catch this. Checking prior art asks the
knowledge base. Arguing from first principles asks the agent itself. An
adversarial review asks another agent. **None of them asks you.** Your intent
entered once, in the opening sentence, and was extrapolated from there.

Grilling is the missing lane: a single concentrated round of questions
*before* the goal is forged, so that execution does not need to interrupt you
later.

## The method

Adapted from [mattpocock/skills](https://github.com/mattpocock/skills)
(`grilling/SKILL.md`, MIT). Three pieces:

**A decision tree.** Write the open questions as a tree, not a list —
decisions depend on decisions, and a child question is only meaningful once
its parent is settled.

**A frontier.** The frontier is every decision whose prerequisites are already
settled, so it can be asked *now*. Ask the whole frontier in one round.
Number each question and **propose an answer** for it — a question with no
recommendation attached transfers your work to the person you are asking.
When the answers come back, recompute the frontier and ask the next round.

**A termination test.** You are done when the frontier is empty — every branch
has been walked, so nothing is left silently assumed — **and** the human has
confirmed. Not before.

## The rule that does the work

> **Anything you can look up, you may not ask.** Facts are the agent's job.
> Decisions are the human's job.

This is what keeps grilling from decaying into a status meeting. "Which
database are we on?" is a fact — go read the config. "Do we accept the
downtime a migration needs?" is a decision — ask.

A fact still being looked up blocks only the questions *downstream* of it. The
rest of the frontier is asked anyway; it does not wait.

## When to run it

Any one of these is enough:

1. Any `done_criteria` entry would come from inference rather than something
   the human said.
2. The work is high-risk.
3. It changes something in production.

If none apply, skip it — but record in the goal file's changelog that you
skipped it and why. A skipped gate that leaves no trace is indistinguishable
from a gate that was never there.

## How it fits with "don't stop mid-run"

These pull in opposite directions only if you run them at the same time.

```
before forging the goal:   frontier empty + human confirmed  → only then forge
after forging the goal:    do not stop except at a real gate
```

Grilling is what earns the second line. The reason an agent feels the pull to
check in every few steps is usually that it was never told what it needed at
the start. Ask everything up front and the interruptions stop being necessary.

## Output

Every decision the grilling settled goes into the goal file's
`done_criteria` — **and `done_criteria` may only be derived from those settled
decisions, never from the agent's own reading of the request.**

---

## Worked shape

```
Round 1 (frontier: 3 open, no prerequisites)

  1. Scope — does this cover the public API only, or internals too?
     Recommend: public API only. Internals have no external contract and
     widening now doubles the surface for a case nobody has asked for.

  2. Compatibility — may the existing on-disk format break?
     Recommend: no. There are users on it; a converter is cheaper than a
     migration everyone has to notice.

  3. Rollout — behind a flag, or straight on?
     Recommend: flag, default off. Same shape as every other rule here:
     observe first, enforce once the findings have earned it.

  (Blocked, not asked this round: the storage-engine question depends on #2.
   Looking up the current format now — that is a fact, not a question for you.)
```

Answers come back, the frontier is recomputed, round 2 asks what #2 unblocked.
When a round produces no new questions, the frontier is empty — confirm, then
forge the goal.
