---
description: Toggle a rule between observe and enforce
---

Toggle a single rule's enforcement mode. Modes have one meaning: **observe** records what *would* have been blocked without stopping the tool; **enforce** blocks the tool outright.

1. Run the CLI to show the current modes:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py modes
   ```
2. Show the user the current mode of every rule.
3. Ask the user to confirm **which rule to toggle** and whether to turn it **on (enforce)** or **off (observe)**. Do not proceed until they confirm both the rule and the direction.
4. Apply the change:
   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/axiom_cli.py enforce <RULE> on|off
   ```
   where `on` selects enforce and `off` selects observe.
5. After the command completes, state the new mode of the toggled rule in one sentence.

Only one rule is toggled per invocation.