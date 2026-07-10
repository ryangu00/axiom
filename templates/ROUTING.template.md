# ROUTING — task-shape → executor policy table (template)

<!-- This file is a TEMPLATE. The value of a routing table is not the table —
     it is the ledger discipline that compiles it. Ship this empty; let your
     own outcomes fill it. Never copy someone else's rows: their cost
     function is not yours. -->

## How this table gets its rows (the method)

1. **Declare before executing.** Every task in a goal file names its `route`
   (who executes: yourself inline, a subagent, a cheaper model, an external
   agent). That declaration is a prediction.
2. **Record what actually happened.** At closeout, append one JSON line to
   `route-outcomes.jsonl`: declared route, actual route, result, whether it
   needed rework, one-line note. Deviations are data, not embarrassments.
3. **Compile lessons into rows.** Periodically (weekly is plenty) read the
   ledger. A pattern that repeats — "shape X failed twice on executor Y" —
   becomes a proposed row.
4. **A human approves every row change.** Proposals go to a
   `ROUTING.proposals.md`; nothing edits this table automatically. This is
   the difference between a self-improving system and a self-corrupting one:
   one bad generalization written by an unattended loop poisons every
   subsequent dispatch decision.

## The table

| Task shape | Route | Quality gate before accepting result | Basis (ledger evidence) |
|---|---|---|---|
| <!-- e.g. bulk mechanical edits across many files --> | <!-- cheapest executor that passed twice --> | <!-- e.g. spot-check 10%, run test suite --> | <!-- e.g. 4/4 clean since 06-15 --> |
| <!-- e.g. security-sensitive change --> | <!-- yourself, always --> | <!-- independent review, never self --> | <!-- policy, not ledger --> |
|  |  |  |  |

## Ledger line format (route-outcomes.jsonl)

```json
{"ts":"<iso>","goal":"<goal-name>","task":"<id>","shape":"<free-text shape>","declared":"<route>","actual":"<route>","result":"done|blocked|wontfix","rework":false,"note":"<one line>"}
```

## Fictional example rows (delete these)

| Task shape | Route | Quality gate | Basis |
|---|---|---|---|
| README/doc drafting from an outline | cheap-model subagent | human reads full output before commit | 6/7 accepted since 06-01; 1 rework was tone |
| cross-file rename with tests present | cheap-model subagent | full test suite green | 5/5 clean |
| anything touching auth or secrets | self, inline | independent reviewer + no self-review | policy row — exempt from ledger pressure |
