/**
 * pi-runtime — PRISM's internal inference agent (task ac69ee28).
 *
 * Runs a pi-agent-core Agent loop on the LOCAL micro model with PRISM
 * tools bridged through the whitelisted POST /api/agent/tool. Spawned by
 * prism_service/inference/pi_agent.py (one JSON job on stdin, one JSON
 * result on stdout — exit 0 even on model failure, error in the payload).
 *
 * Lives in web/ so bare imports resolve from the SPA's node_modules
 * (@earendil-works pi 0.80.3 already installed) — zero extra install.
 *
 * pi toolkit: https://github.com/badlogic/pi-mono (MIT, Mario Zechner).
 */

import { Agent } from "@earendil-works/pi-agent-core";
import { createModels, createProvider } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

// PI's PRISM expertise — ONE shared source for the system prompt + the
// full tool catalog, consumed by this runner AND the SPA rail panel
// (task e70cdcda). Never re-declare a tool schema here.
import { EXPERT_SYSTEM_PROMPT, EXPERT_TOOL_DEFS } from "./pi-expert.mjs";

// Shared multi-call parser (task c4bb21f8): ONE source for both PI
// interception surfaces so an array / concatenated tool-call block parses
// identically here and in the SPA panel. Returns an ORDERED LIST.
import { parseTextToolCall, parseTextToolCalls } from "./pi-toolcall.mjs";

// ------------------------------------------------------------- model

function buildModel(modelId, baseUrl) {
  return {
    id: modelId,
    name: modelId,
    api: "openai-completions",
    provider: "pi-local",
    baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 32768,
    maxTokens: 2048,
    compat: {
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
      supportsStrictMode: false,
    },
  };
}

function buildModels(model, baseUrl) {
  const models = createModels();
  models.setProvider(createProvider({
    id: "pi-local",
    name: "Local (OpenAI-compatible)",
    baseUrl,
    // Keyless local server — empty ModelAuth reads as unconfigured, so
    // resolve a dummy key (Ollama/vLLM/LM Studio ignore Authorization).
    auth: { apiKey: { name: "local (keyless)", resolve: async () => ({ auth: { apiKey: "local-keyless" }, source: "local" }) } },
    models: [model],
    api: openAICompletionsApi(),
  }));
  return models;
}

// -------------------------------------------------- PRISM tool bridges

// The catalog is the shared expert module's — one source for both
// surfaces (task e70cdcda). Local alias keeps the call sites readable.
const TOOL_DEFS = EXPERT_TOOL_DEFS;

// The runner is an INTERNAL caller by construction — every bridged call
// carries internal=true (task 9f20b605). Task e70cdcda retired the
// internal-only gate in api/agent.py (PI IS the orchestrator, panel
// included); the flag stays as harmless caller provenance.
function bridgeBody(name, params) {
  return { name, args: params ?? {}, internal: true };
}

function cap(text, n = 6000) {
  const s = typeof text === "string" ? text : JSON.stringify(text, null, 1);
  return s.length > n ? `${s.slice(0, n)}\n…(truncated)` : s;
}

function buildTools(job, receipts) {
  const names = (job.allowed_tools && job.allowed_tools.length)
    ? job.allowed_tools : ["brain_search", "memory_recall"];
  return names.filter((n) => TOOL_DEFS[n]).map((name) => ({
    name,
    ...TOOL_DEFS[name],
    execute: async (_id, params) => {
      const t0 = Date.now();
      const res = await fetch(
        `${job.api_base}/api/agent/tool?project=${encodeURIComponent(job.project)}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(bridgeBody(name, params)),
        },
      );
      const ms = Date.now() - t0;
      if (!res.ok) {
        receipts.push({ name, ms, ok: false });
        throw new Error(`${name} -> HTTP ${res.status}`);
      }
      const payload = await res.json();
      receipts.push({ name, ms, ok: true });
      return { content: [{ type: "text", text: cap(payload.result) }], details: { ms } };
    },
  }));
}

// -------------------------------------- text tool-call interception

function messageText(message) {
  const c = message?.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) {
    return c.filter((p) => p?.type === "text").map((p) => p.text).join("");
  }
  return "";
}

// parseTextToolCall / parseTextToolCalls now live in the shared
// pi-toolcall.mjs module (imported above) — single object, fenced object,
// top-level array, and concatenated objects all parse to an ordered list.

// ---------------------------------------------------------------- run

async function runJob(job) {
  const started = Date.now();
  const baseUrl = (job.base_url || "http://localhost:11434/v1").replace(/\/$/, "");
  const modelId = job.model || "qwen3:0.6b";
  const model = buildModel(modelId, baseUrl);
  const models = buildModels(model, baseUrl);
  const receipts = [];
  const tools = buildTools(job, receipts);
  // PI ships pre-loaded as the PRISM expert (task e70cdcda): an empty
  // job.system runs with the shared expert prompt, never a blank slate.
  let system = job.system || EXPERT_SYSTEM_PROMPT;
  if (modelId.startsWith("qwen3") && !system.includes("/no_think")) {
    system = `${system} /no_think`.trim();
  }

  const agent = new Agent({
    initialState: { systemPrompt: system, model, thinkingLevel: "off", tools },
    streamFn: (m, c, o) => models.streamSimple(m, c, o),
    getApiKey: () => "local-keyless",
  });

  let turns = 0;
  agent.subscribe((e) => { if (e.type === "turn_end") turns++; });

  const lastAssistantText = () => {
    const msgs = agent.state.messages;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i]?.role === "assistant") return messageText(msgs[i]);
    }
    return "";
  };

  await agent.prompt(job.prompt);
  if (agent.state.errorMessage) {
    return { ok: false, error: agent.state.errorMessage, text: lastAssistantText(),
             turns, tools_used: receipts, ms: Date.now() - started, tokens: 0, model: modelId };
  }

  // Micro-model reality (proven on the panel, Ollama 0.30.7): tool calls
  // arrive as PLAIN JSON TEXT. A MULTI-step ask arrives as the WHOLE plan
  // in ONE block — a top-level array or several concatenated objects (task
  // c4bb21f8). Parse the block into an ordered list, execute each KNOWN
  // tool in order (unknown names skipped with a noted receipt), feed the
  // combined results back, then let the model emit its final answer.
  // `budget` is a TOTAL-call cap across the whole exchange (runaway guard).
  let budget = Number.isFinite(job.intercept_budget) ? job.intercept_budget : 6;
  for (;;) {
    const calls = parseTextToolCalls(lastAssistantText());
    if (!calls.length || budget <= 0) break;
    const results = [];
    for (const call of calls) {
      if (budget <= 0) {
        results.push(`[${call.name} skipped: intercept budget exhausted]`);
        continue;
      }
      const tool = tools.find((t) => t.name === call.name);
      if (!tool) {
        results.push(`[${call.name} skipped: not a known PRISM tool]`);
        continue;
      }
      budget--;
      try {
        const res = await tool.execute(`text-${Date.now()}-${results.length}`, call.args);
        results.push(`[${call.name} result]\n${res.content.map((c) => c.text).join("\n")}`);
      } catch (err) {
        results.push(`[${call.name} error] ${err?.message ?? err}`);
      }
    }
    await agent.prompt(
      `${results.join("\n\n")}\n\nContinue. If you have enough evidence, emit your FINAL answer now in the exact output format the task specified. Do not call another tool unless strictly necessary.`,
    );
    if (agent.state.errorMessage) break;
  }

  const text = lastAssistantText();
  return {
    ok: true, text, turns, tools_used: receipts,
    ms: Date.now() - started,
    tokens: Math.round(text.length / 4), // approximation; usage not surfaced per-run
    model: modelId,
  };
}

// --------------------------------------------------------------- main

async function main() {
  if (process.argv.includes("--check")) {
    // Offline validation: imports resolved, model + tools construct.
    const model = buildModel("check-model", "http://localhost:0/v1");
    buildModels(model, "http://localhost:0/v1");
    const receipts = [];
    const tools = buildTools(
      { allowed_tools: Object.keys(TOOL_DEFS), api_base: "http://localhost:0", project: "check" },
      receipts,
    );
    if (tools.length !== Object.keys(TOOL_DEFS).length) throw new Error("tool construction failed");
    if (!parseTextToolCall('{"name": "brain_search", "arguments": {"query": "x"}}')) {
      throw new Error("interception parser failed");
    }
    // Multi-call block interception (task c4bb21f8): a top-level ARRAY and
    // concatenated objects must each parse to an ordered list of 2 calls.
    const arrCalls = parseTextToolCalls(
      '[{"name":"brain_search","arguments":{"query":"x"}},{"name":"task_create","arguments":{"title":"t"}}]');
    const catCalls = parseTextToolCalls(
      '{"name":"brain_search","arguments":{"query":"x"}}\n{"name":"task_create","arguments":{"title":"t"}}');
    if (arrCalls.length !== 2 || catCalls.length !== 2) {
      throw new Error("multi-call interception parser failed");
    }
    // Surface the bridge body the SAME helper builds for live execute, so
    // tests can prove the internal flag rides every call (task 9f20b605).
    process.stdout.write(JSON.stringify({
      ok: true,
      tools: tools.map((t) => t.name),
      // Multi-call parse report (task c4bb21f8): array + concatenated each
      // yield an ordered list of 2 — the shape the single-object parser dropped.
      multicall: { array: arrCalls.length, concatenated: catCalls.length },
      bridge_body: bridgeBody("conductor_advance", { id: "check" }),
      // The shared expert prompt the run path defaults to when job.system
      // is empty (task e70cdcda) — surfaced so tests prove it's wired.
      expert_prompt_chars: EXPERT_SYSTEM_PROMPT.length,
    }));
    return;
  }

  let raw = "";
  process.stdin.setEncoding("utf-8");
  for await (const chunk of process.stdin) raw += chunk;
  let job;
  try {
    job = JSON.parse(raw);
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: `bad job JSON: ${err?.message}` }));
    return;
  }
  let result;
  try {
    result = await runJob(job);
  } catch (err) {
    result = { ok: false, error: String(err?.stack ?? err), tools_used: [], turns: 0, ms: 0, tokens: 0 };
  }
  process.stdout.write(JSON.stringify(result));
}

main().then(
  () => process.exit(0),
  (err) => { process.stderr.write(String(err?.stack ?? err)); process.exit(1); },
);
