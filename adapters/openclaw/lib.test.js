// `node --test` unit tests for the pure decision logic (no OpenClaw SDK, no
// network). The full handler (CLI shell-out + real consumption + re-entry cap)
// is covered by the author-run p3 probe.
import assert from "node:assert/strict";
import { test } from "node:test";

import { verifyDecision } from "./lib.js";

test("failed claim -> revise with capped retry", () => {
  const d = verifyDecision({
    outcome: "failed",
    reason: "missing artifact",
    claim_id: "c1",
  });
  assert.equal(d.action, "revise");
  assert.equal(d.retry.instruction, "missing artifact");
  assert.equal(d.retry.maxAttempts, 1);
  assert.ok(d.retry.idempotencyKey.includes("c1"));
});

test("failed claim without reason still revises with a default", () => {
  const d = verifyDecision({ outcome: "failed" });
  assert.equal(d.action, "revise");
  assert.ok(d.reason);
});

for (const outcome of ["passed", "no_active_claim", "error"]) {
  test(`${outcome} -> finalize`, () => {
    assert.equal(verifyDecision({ outcome }).action, "finalize");
  });
}

test("null response (CLI unavailable) -> finalize (fail open)", () => {
  assert.equal(verifyDecision(null).action, "finalize");
});
