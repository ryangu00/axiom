# Calibration: what has been measured, and what has not

The README commits to publishing false-positive and false-negative rates for
Axiom in v1.2. This page is the state of that commitment as of 2026-10-03.

**It is not met.** What exists is a calibration of the private hook that
Axiom's `write-verify` derives from, on one operator's sessions, and it
measures less than its headline numbers suggest. It is written down anyway
because a partial result with its limits attached is more use than a promise,
and because the most informative thing in it is a negative finding.

Everything below is aggregate counts from one operator's workload. No
transcript content is published, and the corpus is private, so nobody else can
re-run it. That alone keeps it short of the standard the README holds up
([nah](https://github.com/manuelschipper/nah) calibrates on a public corpus).

## What was measured

Not the hook in this repository.

The predecessor checks, after each individual write call, whether the file is
there, whether the content landed, and whether a commit landed. The public
`write-verify` checks predicates declared in advance, once, when the turn
tries to end. Same distrust, different trigger surface — so numbers about one
are not numbers about the other. And there is no firing data from the public
hook at all yet: the operator's own loops still run the predecessor.

## Corpus and mining

- **5,509 session transcript files.** Files, not sessions: sub-agent and
  worker transcripts are separate files, so the number of independent sessions
  is smaller and was not counted.
- **12,614 write calls** in them (file write, edit, multi-edit, notebook
  edit). The hook's selector treats 12,007 as in scope.
- **One runtime's transcript format.** Writes made through shell redirection
  are not in the corpus.

A candidate is a write call that did not itself report an error but was
followed, within 90 seconds, by a failure signal on the same path.

The first definition of "failure signal" included a further edit to the same
file. That produced 4,811 candidates, about 96% of them false alarms, and was
dropped on the spot. With error signals only, **524 candidates** remained.

## The negative finding

Of those 524, the number that were "the write reported success and the file
is not there" is **0**.

What they were instead: about 382 escaping drift and miscellaneous,
83 a later shell command failing, 40 edit conflicts, 11 edits whose target
string did not match, 8 edits attempted before the file had been read. All of
them are "the file is there and a content-level operation collided" — ordinary
work. (The bucketing was done by the orchestrating model, not by an
independent human pass.)

So in this workload the silent failure the predecessor was built for is rare
enough that mining 12,614 write calls surfaced no natural instance of it. That
is worth knowing. It is **not** a false-negative rate: with no natural
positives there is no denominator to compute a miss rate from.

## The labeled set and the replay

A seed set of 20 cases (4 positives, 5 negatives, the rest cases the selector
should leave alone) was expanded to 70: 4 positives, 55 negatives, 11
should-skip cases.

Replay of the expanded set: 4 true positives, 0 false positives, 0 false
negatives, 55 true negatives; all 11 skip cases skipped.

Read that with the following in hand:

- **Three of the four positives are synthetic.** One comes from a real
  incident involving concurrent sessions. The expansion added no positives.
- **The negatives are the easy kind.** Fifty were mined from real sessions in
  which the file was confirmed present (30 escaping drift, 16 edit conflicts,
  4 target-string mismatches); five are seed cases. The false-positive shapes
  actually met in live use, listed below, are not among them.
- **The verifier was not run.** The replay replaces it with a stub that
  returns the labeled ground truth. What the replay answers is "given the true
  state of the file, do the selector and the trigger logic decide correctly?"
  It is not an end-to-end error rate.
- **The labels are not independent.** The mining script only proposes; labels
  were assigned by the orchestrating model and the operator. There is no
  second annotator and no agreement statistic.

Precision 1.0 and recall 1.0 are therefore arithmetic on four positives, and
should not be quoted on their own. The predecessor's own bar for tuning any of its three behavioral constants was 30
positives; at 4, all three are marked uncalibrated and frozen.

## Live firings

One batch of live firings has been labeled: 50 records from the first three
days after the predecessor went live (2026-07-05 to 2026-07-07).

| Blocking check (32 records) | |
|---|---|
| Could not be labeled | 11 |
| True positive, duplicate (the same fact recorded again on a second stop) | 7 |
| True positive, concurrent overwrite | 5 |
| False positive, parser bug since fixed | 4 |
| False positive, the session had legitimately overwritten its own earlier content | 2 |
| Uncertain | 2 |
| True positive, other | 1 |

| Warn-only check (18 records) | |
|---|---|
| Uncertain | 13 |
| False positive | 5 |

This is not a steady-state false-positive rate and should not be turned into
one: 13 of the 32 blocking records are unlabeled or uncertain, there was one
annotator, and the window includes a parser bug that was fixed afterwards.

Nothing has been labeled since. A further 50 records (2026-08-02 to
2026-09-20) sit in the queue, and the labeling pipeline was paused because
nothing downstream was consuming its output. As of 2026-10-03 the ledger holds
176 firings of the blocking check across 34 sessions (90 at stop, 72 on a
second stop pass, 14 after a tool call) and 93 of the warn-only check. An
audit on 2026-09-21 found that all 17 second-pass stops in the preceding 30
days were released by the re-entry rule without re-checking whether the
problem had been fixed — the same one-block-per-cycle trade this repository
documents for its own hook.

## False-positive sources met in live use

All in the predecessor, all fixed there unless noted:

- Several edits to one path were not de-duplicated, so content from an earlier
  version that had been legitimately overwritten was still required to be in
  the file.
- A redirect target was truncated at a hidden-directory name.
- Redirect syntax inside a quoted string was treated as a write that had
  executed.
- One false-negative source: the de-duplication logic could, for a time,
  launder a real failure into a pass.
- Not fixed, not investigated: on 2026-10-03 a commit made in a scratch
  repository was reported as not having landed, apparently by comparison with
  a different repository's head. One observation.

## Known biases

1. One operator. Varied work, but n=1, and the unit is transcript files.
2. The object measured is the predecessor's trigger logic. The public hook has
   zero live data.
3. One runtime's transcripts; shell-redirect writes excluded. Where these docs
   mention calibration material spanning more than one runtime, that is a
   different rule's replay set, not this corpus.
4. Four positives, three synthetic.
5. Negatives drawn from cases where the file was known to exist.
6. The verifier was stubbed in the replay.
7. Labels by the author side only.
8. Live labels cover the first three days and stop.

## What would meet the commitment

Firings from the hook this repository ships, in real loops. Natural positives
in the tens, not four. Labels from someone other than the author, or at least
a second pass with an agreement figure. A replay that runs the real verifier.
And a corpus a stranger can re-run.

None of those exists yet. Until they do, the v1.2 line stays a commitment, and
observe mode on your own loops remains the only calibration that applies to
you.
