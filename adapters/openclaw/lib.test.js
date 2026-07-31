// `node --test` unit tests for the pure decision logic (no OpenClaw SDK, no
// network). The full handler (CLI shell-out + real consumption + re-entry cap)
// is covered by the author-run p3 probe.
import assert from "node:assert/strict";
import { test } from "node:test";

import { verifyDecision } from "./lib.js";

test("failed + enforced -> revise with capped retry", () => {
  const d = verifyDecision({
    outcome: "failed",
    enforced: true,
    reason: "missing artifact",
    claim_id: "c1",
  });
  assert.equal(d.action, "revise");
  assert.equal(d.retry.instruction, "missing artifact");
  assert.equal(d.retry.maxAttempts, 1);
  assert.ok(d.retry.idempotencyKey.includes("c1"));
});

test("failed + enforced without reason still revises with a default", () => {
  const d = verifyDecision({ outcome: "failed", enforced: true });
  assert.equal(d.action, "revise");
  assert.ok(d.reason);
});

test("failed in observe mode (install default) -> finalize, never revise", () => {
  // CONTRACTS §5: enforced is the authoritative signal; the CLI has already
  // recorded the finding. Missing or false must both stay silent.
  assert.equal(
    verifyDecision({ outcome: "failed", enforced: false, reason: "x" }).action,
    "finalize",
  );
  assert.equal(verifyDecision({ outcome: "failed", reason: "x" }).action, "finalize");
});

test("only a real boolean true enables a revise", () => {
  // A cross-family review flagged that truthiness would let the STRING
  // "false" enable enforcement, and that the three shims only agreed on the
  // missing-key case by coincidence. Pin it: anything that is not the boolean
  // true means not enforced, on every shim.
  for (const value of ["false", "true", 1, 0, null, undefined, {}, []]) {
    assert.equal(
      verifyDecision({ outcome: "failed", enforced: value, reason: "x" }).action,
      "finalize",
      `enforced=${JSON.stringify(value)} must not enable a revise`,
    );
  }
  assert.equal(
    verifyDecision({ outcome: "failed", enforced: true, reason: "x" }).action,
    "revise",
  );
});

for (const outcome of ["passed", "no_active_claim", "error"]) {
  test(`${outcome} -> finalize`, () => {
    assert.equal(verifyDecision({ outcome }).action, "finalize");
  });
}

test("null response (CLI unavailable) -> finalize (fail open)", () => {
  assert.equal(verifyDecision(null).action, "finalize");
});
