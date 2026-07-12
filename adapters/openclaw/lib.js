// Pure handler logic for the OpenClaw Axiom adapter — no OpenClaw SDK import,
// so it is unit-testable with `node --test` and drivable by the p3 probe.
//
// OpenClaw consumes `before_agent_finalize` through
// runAgentHarnessBeforeAgentFinalizeHook -> normalizeBeforeAgentFinalizeResult,
// which turns {action:"revise", retry:{instruction, idempotencyKey, maxAttempts}}
// into an actual turn revision and enforces the re-entry cap via a per-run retry
// budget keyed by idempotencyKey. So this adapter is a thin translation over the
// shared axiom-adapter-cli/v1 primitive:
//
//   session_start          -> `axiom register`  (goal-file discovery)
//   before_agent_finalize  -> `axiom verify`    (failed -> revise, else finalize)
//
// Per CONTRACTS.md §5 the CLI returns facts only; the host mapping lives here.
// Everything fails open: any error lets the turn finalize.

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
// Dev fallback only: valid when this file runs from inside the repo tree. A
// copy-install detaches it, so installed use must set AXIOM_CLI (or have
// `axiom` on PATH). See README.
const DEV_CLI = path.resolve(HERE, "..", "..", "scripts", "axiom_cli.py");

function whichAxiom() {
  try {
    const bin = process.platform === "win32" ? "where" : "which";
    const out = execFileSync(bin, ["axiom"], { encoding: "utf8" });
    return out.trim().split("\n")[0] || null;
  } catch {
    return null;
  }
}

export function cliPath() {
  return process.env.AXIOM_CLI || whichAxiom() || DEV_CLI;
}

function cwdOf(event, ctx) {
  return (
    (event && (event.cwd || event.workspaceDir)) ||
    (ctx && ctx.workspaceDir) ||
    process.cwd()
  );
}

export function callCli(verb, cwd) {
  const cli = cliPath();
  if (!existsSync(cli)) {
    process.stderr.write(`axiom openclaw adapter: CLI not found at ${cli}\n`);
    return null;
  }
  try {
    const out = execFileSync("python3", [cli, verb], {
      input: JSON.stringify({ cwd }),
      encoding: "utf8",
      timeout: 120000,
    });
    const parsed = JSON.parse(out);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

// CLI response -> OpenClaw before_agent_finalize decision. Pure; unit-tested.
export function verifyDecision(response) {
  if (response && response.outcome === "failed") {
    const reason = response.reason || "Axiom verification failed.";
    return {
      action: "revise",
      reason,
      retry: {
        instruction: reason,
        // Stable across the retry so OpenClaw's budget caps at maxAttempts.
        idempotencyKey: `axiom-verify:${response.claim_id || "claim"}`,
        maxAttempts: 1,
      },
    };
  }
  // passed / no_active_claim / error / unavailable -> let the turn finalize.
  return { action: "finalize" };
}

export async function beforeAgentFinalize(event, ctx) {
  return verifyDecision(callCli("verify", cwdOf(event, ctx)));
}

export async function sessionStart(event, ctx) {
  callCli("register", cwdOf(event, ctx));
  return undefined; // session_start carries no decision
}
