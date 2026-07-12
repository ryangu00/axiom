# Axiom adapter for OpenClaw

Wires Axiom's completion-claim verification into
[OpenClaw](https://github.com/openclaw/openclaw) as a **native plugin**.
OpenClaw's `before_agent_finalize` hook can revise a turn: its return is
consumed by `runAgentHarnessBeforeAgentFinalizeHook`, which turns
`{action:"revise", retry:{instruction, idempotencyKey, maxAttempts}}` into an
actual re-run and enforces the re-entry cap through a per-run retry budget. So
this adapter is a thin translation over the shared `axiom-adapter-cli/v1`
primitive — not a second engine.

## What it does

| OpenClaw hook | Verb | Behavior |
|---|---|---|
| `session_start` | `register` | Discovers a `*.goal.md` acceptance block and registers the claim. No turn effect. |
| `before_agent_finalize` | `verify` | On a **failed** claim returns `{action:"revise", retry:{instruction:reason, idempotencyKey, maxAttempts:1}}` so the turn re-runs with the reason. On pass / no-claim / error returns `{action:"finalize"}`. |

The `maxAttempts:1` + a stable `idempotencyKey` (the claim id) mean OpenClaw's
retry budget caps the adapter at one revision per claim. Everything fails open:
any adapter error lets the turn finalize.

## Install

```sh
openclaw plugins install /path/to/axiom-oss/adapters/openclaw
```

`before_agent_finalize` is a raw-conversation hook, so OpenClaw requires
non-bundled plugins to opt in explicitly. Enable the plugin and grant it:

```json
// openclaw.json
{
  "plugins": {
    "entries": {
      "axiom": { "enabled": true, "hooks": { "allowConversationAccess": true } }
    }
  }
}
```

**Point the adapter at the shared CLI.** A copy-install detaches the plugin
from the repo, so set `AXIOM_CLI` (or have `axiom` on `PATH`):

```sh
export AXIOM_CLI=/path/to/axiom-oss/scripts/axiom_cli.py
```

The adapter shells out to `python3 <AXIOM_CLI> verify`; when the CLI cannot be
found it fails open (the turn finalizes) and logs to stderr.

## Declare a claim

Drop a `*.goal.md` in the working directory with an `## acceptance` block
(fenced JSON, predicate `type` field — see
[CONTRACTS.md §1](../../docs/CONTRACTS.md)):

````markdown
## acceptance
```json
[{"type": "file_exists", "path": "dist/bundle.js"}]
```
````

## Uninstall

```sh
openclaw plugins uninstall axiom
```

State lives under the OpenClaw data root, not your project tree.
