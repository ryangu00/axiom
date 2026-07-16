# FAQ

The questions a skeptical reader asks first. Answered with what the code does,
not with adjectives.

## "Isn't this just a prompt telling the model to check its work?"

No — there is no model in the verification path at all. `write-verify` is a
`Stop` hook: a Python process that reads the registered claim, re-runs the
declared predicates against the filesystem (`stat`, a regex, a SHA-256, an
`exit` code), and returns a `decision: block` to the host. It cannot be
talked out of a result, because nothing is talking to it. Same input, same
answer, every time, no tokens.

Run [`scripts/demo.sh`](../scripts/demo.sh): it prints the actual decision
JSON, including a block on a test the agent *said* passed.

## "You didn't invent this."

Correct. `file_exists` / regex / hash / exit code are decades-old primitives —
[deliberately](../README.md#the-one-idea). What's different is *where they
live*: declared before the work, snapshotted into a baseline at registration,
carried across sessions as a claim with an identity, re-verified through a
fresh channel at the loop boundary.

Nor are we first in the neighborhood. [groundtruth](https://github.com/vnmoorthy/groundtruth)
is the closest project and is **ahead of us on calibration**;
[claimcheck](https://github.com/ojuschugh1/claimcheck) does automatic claim
extraction, which we do not, and it's credited on our roadmap;
[tdd-guard](https://github.com/nizos/tdd-guard) enforces a different discipline
at the same hook level and decides with a model where we decide with predicates;
[nah](https://github.com/manuelschipper/nah) sets the calibration bar we
haven't met. All of it, with access dates and what we took from whom:
[PRIOR-ART.md](PRIOR-ART.md).

## "A sandbox is the real answer."

Yes. We agree, and nothing here argues otherwise. A sandbox contains what an
agent *can do*; Axiom audits what an agent *says it did*. Those are different
questions, and a sandbox answers neither of them for you at 3am when the loop
reports "done" on work that never happened.

Axiom is for the loops you run outside a sandbox — which, honestly, is most of
them. It catches the careless false "done," not an agent actively evading it.
The full bypass surface is enumerated in
[KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md#what-this-wont-catch), including
the fact that an agent with filesystem access can delete its own claim.

## "Won't Anthropic just build this in?"

Partly, probably — and that's fine. Claude Code's `/goal` already closes loops
on a stopping condition, judged by a small model that
[reads the conversation](https://code.claude.com/docs/en/hooks). That's the
distinction: an official completion judge asks the model to assess itself;
Axiom is a deterministic audit *outside* the model, against the filesystem.

Everything here sits on the official hook API and above `/goal` on purpose —
built to ride the roadmap, not race it. Where the platform absorbs a piece,
you lose nothing you were depending on, because the piece it absorbs is the
piece you'd stop needing.

## "Stop hooks aren't reliable."

A fair challenge, and the reason we test the seam instead of asserting it.
Every hook has a contract test that invokes it exactly as the host does —
`python3 hooks/<hook>.py --data-root <dir>` with a JSON payload on stdin — and
asserts the decision JSON and the ledger side effects, because a hook whose
`main()` is broken fails open silently and a test that only calls the inner
function would never notice.

The block path is deterministic (`decision: block` + reason on stdout), not a
polite request. Verified host versions are pinned in
[ADAPTERS.md](ADAPTERS.md#verified-host-versions); every adapter fails open on
anything it doesn't understand, and says so on stderr when it does — a host
change degrades to "the agent proceeds, and the failure is visible," never to
a wedged agent and never to a silent one.

If the hook doesn't fire, Axiom does nothing at all — which is exactly why
observe mode exists: you find out what it *would* have caught in your own
loops before you rely on it.

## "No benchmark, no evidence."

Also fair, and stated in [Honest limits](../README.md#honest-limits) rather
than buried. Axiom's thresholds are calibrated on one operator's workload —
months of daily use across four execution lanes, varied but **n=1**. A
neighbor ([nah](https://github.com/manuelschipper/nah)) calibrates against a
public corpus of 101,194 tool calls; that is the better standard, and we say
so instead of matching it rhetorically.

What we offer instead of a number we can't back:

- **Observe mode**, which makes you the benchmark. It records what it would
  have blocked in *your* loops and blocks nothing until you say so.
- **A 30-second reproduction** anyone can run: `scripts/demo.sh`.
- **A commitment with a version on it:** published false-positive/
  false-negative rates in v1.2 — the same bar we hold everyone else to.
