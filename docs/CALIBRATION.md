# Calibration: what has been measured, and what has not

The README commits to publishing false-positive and false-negative rates for
Axiom in v1.2. This page is the state of that commitment as of 2026-10-03.

**It is not met.** What exists is a calibration of the private hook that
Axiom's `write-verify` derives from, on one operator's sessions, and it
measures less than its headline numbers suggest. It is written down anyway
because a partial result with its limits attached is more use than a promise,
and because it contains a negative finding worth having once its own limit is
stated.

Everything below is aggregate counts from one operator's workload. No
transcript content is published, and the corpus is private, so nobody else can
re-run it. That alone keeps it short of the standard the README holds up
([nah](https://github.com/manuelschipper/nah) calibrates on a public corpus).

This page covers `write-verify`. The `stuck-search` threshold has a labeled
replay set of its own, and that one does draw on transcripts from two
runtimes — it is what the README and [ADAPTERS.md](ADAPTERS.md) mean when
they mention calibration material from more than one. None of its figures are
published here: they were recorded against different versions of the data and
do not reconcile with one another, and the set has not been frozen and re-run.
What live data exists for that rule is in
[KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md).

## What was measured

Not the hook in this repository.

Both hooks look at a write twice, and they differ in which look carries the
weight. The predecessor checks each write call as it happens — is the file
there, did the content land, did the commit land — then re-checks everything
it recorded when the turn tries to end, and that is where it blocks; most of
its live firings come from the stop-time pass. The public `write-verify`
blocks only on predicates declared in advance, evaluated when the turn tries
to end. After each write it also records a stat-based read-back in the
ledger, and never blocks on it. Same distrust, different trigger surface — so
numbers about one are not numbers about the other. And none of this is firing
data from the public hook: the machine these numbers were collected on runs
the predecessor and does not have the public plugin installed.

## Corpus and mining

The corpus was mined once, on 2026-07-10, from the transcripts then present
on one machine. The candidate events it produced run from 2026-05-26 to
2026-07-10, about six and a half weeks, with most of them (522 of 614) in the
last three weeks. Many of the early transcripts have since been deleted, so
the corpus cannot be rebuilt from what is on disk today. The
labeled set and the replay below date from the same week. The "as of"
date at the top of this page is the date of this write-up, not of the data.

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
file. That signal alone produced 4,811 false alarms and was dropped on the
spot. With error signals only, **524 candidates** remained.

## The negative finding

Of those 524, the number that were "the write reported success and the file
is not there" is **0**.

What they were instead: about 382 escaping drift and miscellaneous,
83 a later shell command failing, 40 edit conflicts, 11 edits whose target
string did not match, 8 edits attempted before the file had been read. All of
them are "the file is there and a content-level operation collided" — ordinary
work. (The bucketing was done by the orchestrating model, not by an
independent human pass.)

So this mining rule found no natural instance, in the 12,007 write calls the
selector treats as in scope, of the silent failure the predecessor was built for. That is worth knowing, and it
is narrower than it sounds. A candidate needs an error on the same path
within 90 seconds, and a failure that is truly silent may be followed by no
error at all, in which case it never becomes a candidate. The one real
positive in the labeled set below was not mined either: it came from an
incident, and no candidate mentions its path. "Not found by this rule" is the
finding; "rare" is a guess that is consistent with it.

It is also **not** a false-negative rate: with no natural positives there is
no denominator to compute a miss rate from.

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
should not be quoted on their own. The predecessor's own bar for tuning any
of its three behavioral constants was 30 positives; at 4, all three are
marked uncalibrated and frozen.

## Live firings

One batch of live firings has been labeled: 50 records from the first three
days after the predecessor went live (2026-07-05 to 2026-07-07). They are 50
of the 129 firings the ledger held when labeling stopped. Of the 79 left
unlabeled, 77 are among the very first, written before ledger rows carried a
timestamp.

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
nothing downstream was consuming its output. Counted on the afternoon of
2026-10-03, the ledger held 178 firings of the blocking check across 34
sessions (91 at stop, 73 on a second stop pass, 14 after a tool call) and 93
of the warn-only check. The last three of those rows are the
scratch-repository false positive listed below and the two stop passes that
followed it. An audit on 2026-09-21 found that all 17 second-pass stops in
the preceding 30 days were released by the re-entry rule without re-checking
whether the problem had been fixed — the same one-block-per-cycle trade this
repository documents for its own hook.

## False-positive sources met in live use

All in the predecessor, all fixed there unless noted:

- Several edits to one path were not de-duplicated, so content from an earlier
  version that had been legitimately overwritten was still required to be in
  the file.
- A redirect target was truncated at a hidden-directory name.
- Redirect syntax inside a quoted string was treated as a write that had
  executed.
- Not fixed, not investigated: on 2026-10-03 a commit made in a scratch
  repository was reported as not having landed, apparently by comparison with
  a different repository's head. One observation.

One false-negative source belongs beside these, though it was not met in live
use. The fix for the first item could launder a real failure into a pass: an
early write that had failed was folded away by a later, unrelated success on
the same path. A cross-family review of that fix found it with a constructed
case, and it was closed; there is no record of it happening live.

## Known biases

1. One operator. Varied work, but n=1, the unit is transcript files, and the
   corpus is a single snapshot covering about six and a half weeks (mined
   2026-07-10).
2. The object measured is the predecessor's trigger logic. The public hook has
   zero live data.
3. One runtime's transcripts; shell-redirect writes excluded. Where these docs
   mention calibration material spanning more than one runtime, that is the
   `stuck-search` replay set described at the top of this page, not this
   corpus.
4. Four positives, three synthetic.
5. Negatives drawn from cases where the file was known to exist.
6. The verifier was stubbed in the replay.
7. Labels by the author side only.
8. Mining can only see a failure that is followed by an error within 90
   seconds.
9. Live labels are 50 of the 129 firings from the first three days, and stop
   there.

## What would meet the commitment

Firings from the hook this repository ships, in real loops. Natural positives
in the tens, not four. Labels from someone other than the author, or at least
a second pass with an agreement figure. A replay that runs the real verifier.
And a corpus a stranger can re-run.

None of those exists yet. Until they do, the v1.2 line stays a commitment, and
observe mode on your own loops remains the only calibration that applies to
you.
