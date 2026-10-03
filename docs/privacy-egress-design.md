# Design note: a fail-closed egress gate for confidential content (v2 preview)

<!-- Status: design document only. A working implementation runs in the
     author's environment but is coupled to a private knowledge base and is
     NOT shipped in v1. This note documents the architecture because several
     of its decisions were hard-won and generalize. A provider-based,
     shippable implementation is on the v2 roadmap. -->

## Problem

Once your agent can *read* from a knowledge base, a new exfiltration path
opens: confidential content retrieved locally can be handed to a cloud model
(a cheaper-tier dispatch, an external agent CLI, a `curl` to some API) with no
mechanical stop between "retrieved" and "sent." Routing discipline alone does
not close this — discipline is what fails at 2 a.m. in an unattended loop.

## The gate

A `PreToolUse` hook on the dispatch/network surface (Bash, Agent prompts,
writes to auto-pushed directories). Before content can leave, two independent
predicates run:

1. **Content match** — regexes that recognize your confidential shapes.
2. **Label reference** — the retrieved item is tagged confidential and its id
   is being quoted into an outbound payload.

Either one fires → the action is blocked with a one-line remediation.

## Five decisions that generalize

- **Criteria are single-sourced, never copied into the hook.** The gate loads
  its patterns at runtime from the one place they already live. A second copy
  of a confidential-pattern list is itself a second confidential artifact and
  a drift source. The hook file ships with **zero** literal patterns in it.
- **A canary self-test on every run.** Before judging real content, the gate
  runs a harmless constructed string that *must* match. If it doesn't, the
  criteria failed to load — and the gate treats that as failure, not as "all
  clear."
- **Fail-closed on the decision path, fail-open only off it.** If pattern
  loading, regex compilation, or the label query throws, the gate **denies**
  (with a fix hint). Only non-decision work — writing the audit log — is
  allowed to fail silently. (This is the opposite of the completion-verifier's
  posture, where a false block is worse than a missed check. Match the failure
  mode to the stakes.)
- **The per-action override is a single-use, logged token.** An env variable
  set inside the command text is invisible to the hook process anyway (it
  inherits the host's environment, not the command's). So the override for one
  action is a one-shot file token the hook consumes and records — auditable,
  not ambient. An earlier version of this note said "not an env var" and
  stopped there, which claimed too much: the reference implementation also
  honours a variable present in the *host* process's environment, and that one
  is reusable for as long as the process lives. It is a second, explicit
  channel, and it is ambient in exactly the way the token is not. If you build
  this, decide on purpose whether you want both.
- **The canary asserts the path it expects to take, not just the answer.** A
  self-test that returns a boolean can pass for the wrong reason. The story
  behind this one is [below](#the-canary-that-passed-for-the-wrong-reason).

## The canary that passed for the wrong reason

The content check has more than one route to a match: a primary shape check,
and a fallback that re-checks the text with whitespace removed. The original
canary was a key prefix followed by forty copies of the letter `x`. The
placeholder filter on the primary route treats a run of `x` as a redaction
mark and discards it. So the canary never once matched on the primary route.
It matched on the fallback, which has no placeholder filter, and the
self-test — asserting only "it matched" — reported healthy.

That was the state from 2026-07-07, the day before the gate went live, until
2026-08-23: about 47 days in which the canary was not exercising the primary
route at all, and would not have noticed if that route had died.

It surfaced by accident. An unrelated and correct fix — a left boundary on a
prefix-style key pattern, so that ordinary hyphenated words whose last letters
happen to spell a key prefix stop reading as keys — made the fallback stop
matching the canary as well. From that moment the gate denied every outbound
action. It was noticed and repaired the same evening, within about an hour
and a half, after a read-only probe was refused. Fail-closed did its job. The
canary had not been doing its own, and there was no trail to say so: the
canary-failure and criteria-load-failure branches denied without writing to
the ledger, which by then held roughly 950 rows and not one canary record.

The repair was three changes:

1. A canary with a realistic key shape, one the placeholder filter does not
   touch.
2. An assertion on the result *and* on the route that produced it. Asserting
   the boolean alone is permission for the primary route to die quietly.
3. Ledger writes on both deny branches.

Rejected: taking the left boundary back out, adding the placeholder filter to
the fallback route, and downgrading a canary failure to warn-and-allow.

Verified with five cases — the canary takes the primary route, a realistic key
is blocked, a placeholder passes, the earlier false-positive sample passes,
clean text passes — and end to end: the probe that had been refused goes
through, and a command carrying a realistic key shape is stopped.

One limit stays: an override also carries an action past a failed canary. That
is a design trade-off, and worth knowing before you rely on the canary as the
last line.

## Three things a second review measured

On 2026-09-26 a second cross-family review re-tested the fixes that came out
of a first one. It was read-only and offline: synthetic hook input, no command
executed, the label query stubbed, ledger and token paths redirected to a
temporary directory, and the 37 files under review hashed before and after. It
reported no blockers, five major findings and one minor. Three of its results
concern this gate, and each generalizes.

**1. A payload passed by file reference is not in the command text.**
`curl … --data-binary @payload.json` puts the sensitive body in a file; a gate
that reads the command string sees nothing and allows it. The first review
found this, and the first fix passed all 17 of its own regressions. The second
review showed that the rule matched the raw text before shell unquoting, so
these still went through: `-F"f=@P"`, `--form f="@P"`, `-TP` (a short option
fused to the filename), `--data-binary ""@P`, `--data-urlencode "name"@P` —
and `scp` and `rsync`, which were not treated as an outbound channel at all.
Of the ten shapes tabulated, three were denied and seven allowed; the version
before the first fix allowed all 19 probe shapes.

Status: fixed the same day. Decisions are now made on the argument vector
after shell-style unquoting, a copy to a host outside the trusted set counts
as an outbound channel, and a command that will not parse is denied; the
regression set grew to 28 cases, all passing. That fix has **not** been
through a third independent review.

It also produced a new false positive on the day it landed, and that one is
still open: prose inside a heredoc containing the literal word `scp` followed
by another word is blocked as a transfer, because the argument scan does not
tell heredoc text from a command. The rule has not been narrowed.

So there are two statuses here, not one. The upload gap: found, fixed, the
fix not independently re-verified. The false positive the fix introduced:
found, not fixed. The general point stands either way: decide on what the
shell will execute, not on the string that was typed.

**2. A single-use token is single-use only if consuming it is atomic.** In
the first version two hook processes could both see the token; one deleted it,
the other's delete failed, the error was swallowed, and both allowed — one
grant, two passes. The fix is to race on a same-filesystem rename and allow
only the winner. The second review held two separate processes at a barrier
until both had seen the token, released them together, and ran 12 rounds
against each version: the old one allowed both sides in 12 of 12 rounds, the
fixed one allowed exactly one side in 12 of 12. A separate local check
recorded alongside the code: two processes removing the same file at once
both "succeeded" in 3 runs of 5, while rename or exclusive create let exactly
one succeed in 5 of 5.

Limits: this is a synthetic race, not an observed incident. An action that
passes on its own does not consume the token. The result does not cover a
token being re-created concurrently, or another process under the same user
rewriting the hook. And the host-environment override described above is
untouched by any of it.

**3. A local proxy is not a local destination.** The gate gained a
local-network exemption on 2026-08-30 because purely internal calls kept being
blocked. All three conditions must hold: the only hit is the "scripting
language making a network call" channel detection, every host extracted from
the command is on the private network, and there is no sign of encoding
evasion. Three days later, when cloud egress was consolidated behind one
gateway on the local machine, an exception to the exemption followed: a call
to the local model-forwarding proxy is not an internal endpoint, because the
proxy forwards outward, so it falls back to the ordinary content decision.
Both directions were drilled then (local model endpoint allowed, proxy
endpoint blocked), and the second review reproduced it offline, with all
eight existing local-network cases behaving as expected.

The cost is a precision limit of static decision. A command that reads a
credential only to authenticate, sends a public body, and pins a local model
is denied too. The reviewer's recommendation was to keep the denial and change
only the diagnostic, because a loopback address does not prove the whole
fallback chain stays off the cloud.

The same review also found no new allowance across 36 older quoting and
content cases, and all four injected faults — criteria fail to load, canary
fails, label query fails, uncaught exception — ended in a denial.

## Running numbers

From the reference deployment's ledger, 2026-07-08 through 2026-10-03 (87
days). One operator's workload. Aggregates only: the criteria and the labeled
samples are confidential, and since the implementation is not shipped, nobody
else can re-run this.

- **808 block rows, 156 override rows, 91 local-network exemption rows** (the
  exemption has existed only since 2026-08-30).
- **808 is rows, not events.** Collapsing byte-identical rows — same second,
  same session, same basis — leaves **391**. Blocks are bursty: one day
  accounts for 342 rows and one session for 436. In all, 49 sessions and 48
  days saw at least one block.
- **Most blocks are the gate declining to guess.** The conservative rule — the
  command contains command substitution, a file read, or variable indirection,
  so what it will send cannot be decided statically and is treated as a hit —
  accounts for 689 of 808 rows (85.3%) and 284 of 391 distinct rows (72.6%).
  Say which denominator you mean; the two differ by almost thirteen points.
  The other distinct rows: 67 proper-noun matches, 24 key-shaped values, 12
  label references, 2 decision-path errors, 1 combination match, 1 upload
  payload.
- **By channel** (distinct rows): 363 shell commands, 26 dispatches to an
  agent from another model family, 2 other agent calls.
- **Overrides: 156**, of which 127 were against the conservative rule. Against
  391 distinct blocks, overrides are 28.5% of distinct block-or-override
  records; over an eight-week baseline window the same ratio was 93 of 302
  (30.8%). No interval is attached: these records are bursty and come from one
  operator, so they are not independent draws.

**An override is not a false positive.** No override has been labeled right or
wrong, so there is no false-positive rate here, and we do not offer one.

### How false positives were handled

- **Criteria, 2026-07-07.** A sibling write-side gate sharing the same
  criteria source used plain word matching and wrongly blocked 16 of 20
  cases measured. It was replaced by three buckets: value shapes, proper nouns,
  and high-frequency combinations. Two cross-family adversarial rounds both
  found holes. The first: key prefixes of three cloud vendors uncovered, bare
  numeric values passing, and a regression from removing bare words. The
  second: the must-block items held, but the fix had brought in two
  false-positive regressions and two logic bugs. What followed was a ruling
  instead of another round — pierced twice means change the layer, not the
  pattern. Regexes are a high-confidence coarse filter; deliberate evasion
  (encoding, featureless prose) belongs to the label-and-destination layer.
- **Launch day, 2026-07-08.** Three real blocks, all boundary-definition debt:
  the gate blocked its own project's acceptance dispatch (fixed by
  distinguishing sub-agent types), blocked an edit to a document that was
  about the gate (channel detection narrowed to strong signals), and showed
  that an override variable set in the command never reaches the hook
  (replaced by the file token). The pre-launch review had already turned the
  decision path from fail-open — a first-version mistake — to fail-closed.
- **Key shapes, 2026-08-23.** The left-boundary fix described above, with
  variable references counted as placeholders. The whitespace-stripped
  re-check joins text across lines and had been amplifying the false positive;
  removing it was rejected, because that would open a hole for a value split
  across lines. This is the fix that exposed the canary.
- **2026-08-30 to 2026-09-26.** The local-network exemption, the proxy
  exclusion, the upload-payload rule and the atomic token, then argument-level
  upload parsing.

Replay-set scores for the criteria are absent on purpose. The figures recorded
at different times do not reconcile with one another, and we are not going to
publish a confusion matrix we cannot add up.

## Why it's staged, not shipped

The pattern source and the label store are, in the reference deployment, a
private knowledge base. A shippable version abstracts both behind the same
provider interface v1 already uses for memory: a `CriteriaProvider` (you
supply your own pattern file) and an optional `LabelProvider`. Until that
interface is proven against a second real deployment, shipping the coupled
version would violate this project's own honesty rule — so it waits.
