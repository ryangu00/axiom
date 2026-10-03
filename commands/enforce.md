---
description: Toggle a rule between observe and enforce
---

Toggle a single rule's enforcement mode. **Observe** (the default) records
findings without blocking. In **enforce** mode, `write-verify` blocks completion
and `schema-guard` denies matching writes. `preflight` and `stuck-search` only
advise and never block in either mode: observe logs findings, while enforce
injects guidance.

1. Run the CLI to show the current modes:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py modes
   ```
2. Show the user the current mode of every rule.
3. Ask the user to confirm **which rule to toggle** and whether to turn it **on (enforce)** or **off (observe)**. Do not proceed until they confirm both the rule and the direction.
4. Apply the change:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py enforce <RULE> on|off --by human
   ```
   where `on` selects enforce and `off` selects observe. `--by human` is the
   record of step 3: the ledger stores it as `decided_by`, and it is an
   assertion, not something the tool can verify — pass it only after the
   user confirmed. Without `--by` the ledger records `unattested`.
5. After the command completes, state the new mode of the toggled rule in one sentence.

Only one rule is toggled per invocation.
