# Axiom adapter for hermes-agent

Wires Axiom's completion-claim verification into
[hermes-agent](https://github.com/NousResearch/hermes-agent) as a **native
plugin**. hermes exposes the verify loop through the `pre_verify` hook, whose
return is consumed by hermes' own `get_pre_verify_continue_message`; that
function natively accepts `{"action": "continue", "message"}` (and the
Claude-Code Stop shape), so this adapter is a thin translation over the shared
`axiom-adapter-cli/v1` primitive — not a second engine.

## What it does

| hermes hook | Verb | Behavior |
|---|---|---|
| `on_session_start` | `register` | Discovers a `*.goal.md` acceptance block in the working dir and registers the claim. Returns `None` (no turn effect). |
| `pre_verify` | `verify` | Fired once per turn after the agent edits code. On a **failed** claim returns `{"action":"continue","message":reason}` so the turn keeps going with the reason; on pass / no-claim returns `None` and the turn finishes. |

Both fail open: hermes wraps every hook in try/except, and each path returns
`None` on any adapter error, missing CLI, bad JSON, or timeout. `pre_verify`
self-throttles on the `attempt` counter (one nudge per turn), matching the
`axiom-adapter-cli/v1` re-entry cap.

hermes payloads carry **no per-task cwd** (it is the process-global
`TERMINAL_CWD`), so the shim resolves the working directory from `TERMINAL_CWD`
(falling back to the process cwd). Concurrent turns in one gateway share that
value — the same boundary hermes itself has.

## Install

Place (or symlink) this directory as a hermes user plugin and enable it:

```sh
ln -s /path/to/axiom-oss/adapters/hermes ~/.hermes/plugins/axiom
```

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled:
    - axiom
```

**Point the adapter at the shared CLI.** A copy-install detaches the plugin
from the repo, so set `AXIOM_CLI` (or have `axiom` on `PATH`):

```sh
export AXIOM_CLI=/path/to/axiom-oss/scripts/axiom_cli.py
```

A symlink install keeps the repo-relative fallback working. When the CLI cannot
be found the adapter fails open and logs to stderr rather than blocking.

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

Remove the plugin from `plugins.enabled` (or delete the symlink). State lives
under the hermes data root, not your project tree.
