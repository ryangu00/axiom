# Failure modes of long-running agent work — a field taxonomy

<!-- Each mode below was observed repeatedly in months of daily long-horizon
     agent operation before this plugin existed. The hooks in this plugin
     are the mechanical countermeasures; this file is the map. -->

## 1. False success (the flagship)
**Shape:** the agent reports "done / fixed / written" but the environment
shows no corresponding change — file absent, content unchanged, tests never
executed, commit never made. Often not lying so much as confusing *intending*
with *having done*: the plan was generated, the narration followed, the write
step silently failed or was skipped.
**Why it compounds in loops:** unattended loops act on their own reports. One
false "done" becomes the premise of every subsequent step.
**Countermeasure here:** `write-verify` — completion claims are checked
against declared evidence predicates (file state, content, fresh command
runs) at Stop time. Testimony is not evidence.

## 2. Stuck-loop grinding
**Shape:** the same operation fails 3, 5, 8 times with cosmetic variations;
the agent keeps trying "one more fix" instead of stepping back. Burns tokens,
time, and sometimes the environment (retry storms against a wedged service).
**Root cause:** error-fix myopia — each attempt looks locally reasonable;
nothing tracks the *series*.
**Countermeasure:** `stuck-search` — failure fingerprinting across attempts;
at threshold, force a stop-and-search-externally step (the answer to most
environment/toolchain failures already exists in a forum or issue tracker).

## 3. State written to sand
**Shape:** ledgers, configs, or progress files written to temp directories —
gone on reboot, invisible to the next session. The work happened; the memory
of it evaporated.
**Countermeasure:** `schema-guard` — advisory interception when
persistent-looking artifacts (ledger/state/config patterns) target temp paths.

## 4. Irreversible action on a hunch
**Shape:** destructive commands (`rm -rf`, hard resets, force pushes,
`DROP TABLE`) executed mid-flow with the same casualness as a directory
listing, on premises that were never verified.
**Countermeasure:** `preflight` — recognized irreversible shapes trigger an
injected pre-mortem prompt (what breaks, how to roll back, where is the
anchor) before execution.

## 5. Scope creep / gold-plating
**Shape:** the inverse of false success — things nobody asked for get built:
speculative abstractions, extra config systems, "while I'm here" features.
Every unrequested artifact is future maintenance debt with no owner.
**Countermeasure:** process, not hook (v1): the goal template's discipline —
each task must break a done-criterion if deleted; closeout names any extras
and who asked for them.

## 6. Plan drift without a trail
**Shape:** the plan changes mid-flight (often correctly!) but the change
lives only in the context window. After compaction or session loss, the
executed work no longer matches any written intent — un-auditable.
**Countermeasure:** goal-file changelog discipline — every re-plan is one
line (timestamp / change / why) written in the same edit as the change.

## 7. Memory poisoning
**Shape:** the agent's own memory becomes the attack surface or the rot
source: stale facts recalled as current, or imported notes containing
instruction-shaped text that steers later sessions.
**Countermeasure:** provider layer — every recalled lesson carries a
timestamp + source and an explicit "unverified memory" prefix;
instruction-shaped content is quarantined at import.

## 8. Silent guard death
**Shape:** the meta-failure: the enforcement layer itself fails open — a
missing interpreter, a renamed field, a broken hook — and everything looks
fine because nothing is being checked at all. Worse than having no guards:
you *believe* you are covered.
**Countermeasure:** SessionStart health check (loud warning on any dead
hook) + heartbeat events in the ledger so an empty report is distinguishable
from a dead pipeline.
