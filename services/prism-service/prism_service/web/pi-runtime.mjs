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
import { createModels, createProvider, Type } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

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

const TOOL_DEFS = {
  brain_search: {
    label: "Brain search",
    description: "Search the project's code/knowledge base (hybrid BM25 + vector + graph).",
    parameters: Type.Object({ query: Type.String({ description: "Search query" }) }),
  },
  memory_recall: {
    label: "Memory recall",
    description: "Recall the team's long-term memory: conventions, decisions, failures. ALWAYS check here before claiming something is new knowledge.",
    parameters: Type.Object({ query: Type.String({ description: "Natural-language query" }) }),
  },
  task_list: {
    label: "Task list",
    description: "List tasks in the PRISM tracker (optional status filter).",
    parameters: Type.Object({ status: Type.Optional(Type.String()) }),
  },
  prism_status: {
    label: "PRISM status",
    description: "Brain/Graph index health (doc counts, staleness).",
    parameters: Type.Object({}),
  },
};

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
          body: JSON.stringify({ name, args: params ?? {} }),
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

function parseTextToolCall(text) {
  let t = (text || "").trim();
  const fence = /^```(?:json)?\s*([\s\S]*?)\s*```$/.exec(t);
  if (fence) t = fence[1].trim();
  if (!t.startsWith("{") || !t.endsWith("}")) return null;
  try {
    const obj = JSON.parse(t);
    const name = obj?.name;
    const args = obj?.arguments ?? obj?.args ?? obj?.parameters ?? {};
    if (typeof name !== "string" || !name) return null;
    if (typeof args !== "object" || args === null || Array.isArray(args)) return null;
    return { name, args };
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- run

async function runJob(job) {
  const started = Date.now();
  const baseUrl = (job.base_url || "http://localhost:11434/v1").replace(/\/$/, "");
  const modelId = job.model || "qwen3:0.6b";
  const model = buildModel(modelId, baseUrl);
  const models = buildModels(model, baseUrl);
  const receipts = [];
  const tools = buildTools(job, receipts);
  let system = job.system || "";
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
  // arrive as PLAIN JSON TEXT. Execute them app-side and continue, capped.
  let budget = Number.isFinite(job.intercept_budget) ? job.intercept_budget : 4;
  for (;;) {
    const call = parseTextToolCall(lastAssistantText());
    if (!call || budget <= 0) break;
    const tool = tools.find((t) => t.name === call.name);
    if (!tool) break;
    budget--;
    let resultText;
    try {
      const res = await tool.execute(`text-${Date.now()}`, call.args);
      resultText = res.content.map((c) => c.text).join("\n");
    } catch (err) {
      resultText = `ERROR: ${err?.message ?? err}`;
    }
    await agent.prompt(
      `[${call.name} result]\n${resultText}\n\nContinue. If you have enough evidence, emit your FINAL answer now in the exact output format the task specified. Do not call another tool unless strictly necessary.`,
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
    process.stdout.write(JSON.stringify({ ok: true, tools: tools.map((t) => t.name) }));
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
