// OpenClaw plugin entry: wires Axiom's two verbs into OpenClaw's lifecycle.
// The handler logic lives in lib.js (no SDK import) so it stays unit-testable;
// this file only registers it.
import { definePluginEntry } from "openclaw/plugin-sdk/core";

import { beforeAgentFinalize, sessionStart } from "./lib.js";

export default definePluginEntry({
  id: "axiom",
  name: "Axiom verification",
  register(api) {
    // session_start: register the goal-file claim (no turn effect).
    api.on("session_start", sessionStart);
    // before_agent_finalize: verify; a failed claim returns
    // {action:"revise", retry:{...}} so OpenClaw re-runs the turn with the
    // reason. Passing / no-claim / error finalizes.
    api.on("before_agent_finalize", beforeAgentFinalize);
  },
});
