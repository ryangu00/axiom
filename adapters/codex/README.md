# Axiom adapter for Codex CLI

Wires Axiom's completion-claim verification into [Codex
CLI](https://github.com/openai/codex) (>= 0.144.1) as a **native plugin**.
Codex fires Claude-Code-shaped lifecycle hooks and honors the same decision
protocol, so this adapter is a thin translation shim over the shared
`axiom-adapter-cli/v1` primitive — not a second engine. The runtime evidence
that Codex's `Stop` hook really consumes `{"decision":"block"}` is recorded in
the p1 probe; see [ADAPTERS.md](../../docs/ADAPTERS.md).

## What it does

| Codex hook | Verb | Behavior |
|---|---|---|
| `SessionStart` | `register` | Discovers a `*.goal.md` acceptance block in cwd and registers the claim. Never influences the turn. |
| `Stop` | `verify` | Evaluates the active claim fresh. On **failure** returns `{"decision":"block","reason"}` so the turn continues with the reason; on pass / no-claim it is silent. |

Both fail open: any adapter error, missing CLI, bad JSON, or timeout lets the
agent proceed and logs an observable event. A failed verify may drive **at
most one** block re-entry per claim (`stop_hook_active` guard); after that it
fails open and writes a `verify_reentry_capped` event.

Observe-mode advisory rules (`stuck-search`, `schema-guard`, `preflight`) are
not wired here yet — this adapter ships the register/verify evidence chain
only.

## Install

```sh
codex plugin marketplace add /path/to/axiom-oss/adapters/codex
codex plugin add axiom-codex@axiom-codex
```

**Point the adapter at the shared CLI.** Codex copies the plugin into its own
cache on install, detaching it from the repo, so the adapter cannot assume a
relative path. Set `AXIOM_CLI` to the CLI in your clone:

```sh
export AXIOM_CLI=/path/to/axiom-oss/scripts/axiom_cli.py
```

If `axiom` is on your `PATH` the adapter finds it there instead. When neither
is available the adapter fails open (the turn proceeds) and logs an
`adapter_cli_missing` event, so a mis-set path is visible rather than silent.

## Declare a claim

Drop a `*.goal.md` file in your working directory with an `## acceptance`
block (fenced JSON, predicate `type` field — see
[CONTRACTS.md §1](../../docs/CONTRACTS.md)):

````markdown
## acceptance
```json
[{"type": "file_exists", "path": "dist/bundle.js"},
 {"type": "cmd_succeeds", "cmd": ["pytest", "-q"]}]
```
````

## Uninstall

```sh
codex plugin remove axiom-codex@axiom-codex
codex plugin marketplace remove axiom-codex
```

State (claims, ledger) lives under the plugin data root, not in your repo;
removing the plugin stops all hooks. Nothing is left in your project tree.

## Limits (v1)

- The shim's CLI call times out at 120 s. A `cmd_succeeds` predicate that runs
  longer fails open (agent proceeds, event logged) rather than blocking.
- Registration is goal-file discovery only; the direct `register_claim_if_absent`
  path is available through the CLI but not auto-wired to a Codex hook.
