/**
 * PI — PRISM's omnipresent left-rail agent (task 711d5235).
 *
 * Built on Mario Zechner's pi toolkit (MIT):
 *   @earendil-works/pi-ai         — unified LLM API (OpenAI-compatible local endpoints)
 *   @earendil-works/pi-agent-core — agent loop + tool execution + event stream
 * https://github.com/badlogic/pi-mono — MIT License, Copyright (c) Mario Zechner.
 *
 * Inference is browser -> LOCAL endpoint only (default Ollama :11434); no
 * cloud key exists anywhere in this path (INV-1). The agent's tool calls go
 * through PRISM's whitelisted passthrough POST /api/agent/tool.
 */

import {
  Agent,
  type AgentTool,
} from "@earendil-works/pi-agent-core";
import {
  createModels,
  createProvider,
  Type,
  type Model,
} from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { api } from "@/lib/api";

// ---------------------------------------------------------------- config

export type PiModelChoice = {
  id: string;
  label: string;
  hint?: string;
  /** G5 candidate — smaller than the documented-reliable floor. */
  experimental?: boolean;
};

// Smallest documented tool-calling-reliable model first; qwen3:4b and below
// are exactly what G5 (task 9f8e7bf7, "smallest model PRISM can carry")
// probes with the telemetry this panel records.
export const PI_MODEL_CHOICES: PiModelChoice[] = [
  { id: "qwen2.5-coder:7b", label: "qwen2.5-coder:7b", hint: "recommended · chat + tools" },
  { id: "qwen3:0.6b", label: "qwen3:0.6b", hint: "micro · self-learning bench winner", experimental: true },
  { id: "qwen3:1.7b", label: "qwen3:1.7b", hint: "micro" },
  { id: "llama3.2:3b", label: "llama3.2:3b" },
];

export type PiConfig = { baseUrl: string; modelId: string };

const CONFIG_KEY = "prism.pi.config";
const DEFAULT_CONFIG: PiConfig = {
  baseUrl: "http://localhost:11434/v1",
  modelId: "qwen2.5-coder:7b",
};

export function loadPiConfig(): PiConfig {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    if (raw) return { ...DEFAULT_CONFIG, ...JSON.parse(raw) };
  } catch { /* fall through to default */ }
  return { ...DEFAULT_CONFIG };
}

function savePiConfig(cfg: PiConfig) {
  try { localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg)); } catch { /* best-effort */ }
}

// ------------------------------------------------------------- provider

function buildModel(cfg: PiConfig): Model<"openai-completions"> {
  return {
    id: cfg.modelId,
    name: cfg.modelId,
    api: "openai-completions",
    provider: "pi-local",
    baseUrl: cfg.baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 32768,
    maxTokens: 4096,
    // Local OpenAI-compatible servers (Ollama/vLLM/LM Studio): system role,
    // no reasoning_effort, no strict tool mode.
    compat: {
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
      supportsStrictMode: false,
    },
  };
}

const models = createModels();

function installProvider(cfg: PiConfig): Model<"openai-completions"> {
  const model = buildModel(cfg);
  models.setProvider(createProvider({
    id: "pi-local",
    name: "Local (OpenAI-compatible)",
    baseUrl: cfg.baseUrl,
    // Keyless local server — pi-ai treats an empty ModelAuth as "not
    // configured" ("No API key for provider"), so resolve a dummy key;
    // Ollama/LM Studio/vLLM ignore the Authorization header.
    auth: { apiKey: { name: "local endpoint (keyless)", resolve: async () => ({ auth: { apiKey: "local-keyless" }, source: "local" }) } },
    models: [model],
    api: openAICompletionsApi(),
  }));
  return model;
}

// ------------------------------------------------------- PRISM tools

function toolText(payload: unknown): string {
  const text = typeof payload === "string" ? payload : JSON.stringify(payload, null, 1);
  // Small models drown in giant receipts — cap what goes back to the LLM.
  return text.length > 6000 ? `${text.slice(0, 6000)}\n…(truncated)` : text;
}

type PassthroughResponse = { name: string; result: unknown; raw: string };

function prismTool(
  project: string,
  name: string,
  label: string,
  description: string,
  parameters: AgentTool["parameters"],
): AgentTool {
  return {
    name,
    label,
    description,
    parameters,
    execute: async (_id, params) => {
      const started = performance.now();
      const res = await api.post<PassthroughResponse>(
        `/api/agent/tool?project=${encodeURIComponent(project)}`,
        { name, args: params ?? {} },
      );
      return {
        content: [{ type: "text", text: toolText(res.result) }],
        details: { result: res.result, ms: Math.round(performance.now() - started) },
      };
    },
  };
}

function buildTools(project: string): AgentTool[] {
  return [
    prismTool(project, "brain_search", "Brain search",
      "Search the project's code/knowledge base (hybrid BM25 + vector + graph). Use for any question about the codebase.",
      Type.Object({ query: Type.String({ description: "Search query" }) })),
    prismTool(project, "memory_recall", "Memory recall",
      "Recall the team's long-term memory: conventions, decisions, failures, expertise.",
      Type.Object({ query: Type.String({ description: "Natural-language query" }) })),
    prismTool(project, "memory_store", "Memory store",
      "Store a durable memory entry the team should keep (conventions, decisions, learnings).",
      Type.Object({
        domain: Type.String({ description: "Expertise domain, e.g. conventions, architecture" }),
        name: Type.String({ description: "Short kebab-case name" }),
        description: Type.String({ description: "Detailed description with concrete evidence" }),
        type: Type.String({ description: "pattern | convention | failure | decision" }),
        classification: Type.String({ description: "tactical | foundational | strategic" }),
      })),
    prismTool(project, "task_list", "Task list",
      "List tasks in the PRISM tracker. Optionally filter by status: pending, in_progress, done, blocked.",
      Type.Object({ status: Type.Optional(Type.String({ description: "Status filter" })) })),
    prismTool(project, "task_create", "Task create",
      "Create a new task in the PRISM tracker.",
      Type.Object({
        title: Type.String({ description: "Short human-friendly title" }),
        description: Type.Optional(Type.String({ description: "Details" })),
      })),
    prismTool(project, "prism_status", "PRISM status",
      "Check the project's Brain/Graph index health (doc counts, staleness).",
      Type.Object({})),
  ];
}

const SYSTEM_PROMPT = [
  "You are PI, PRISM's omnipresent assistant living in the app's left rail.",
  "PRISM is a software-engineering knowledge service (brain index, long-term memory, task tracker, conductor SDLC).",
  "Use your tools to answer from the project's REAL data — brain_search for code questions, memory_recall for conventions/decisions, task_list for work state. Never invent project facts a tool could check.",
  "Be concise: short paragraphs, no filler. You run on a small local model — prefer one good tool call over many.",
].join(" ");

// ------------------------------------------------------------ the store

export type PiItem =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string; done: boolean; learned?: boolean; learning?: boolean }
  | { kind: "tool"; callId: string; name: string; label: string; args: unknown;
      status: "running" | "ok" | "error"; result?: unknown; ms?: number }
  | { kind: "note"; text: string };

export type PiTelemetry = { model: string; ok: boolean; at: number };

/**
 * Module-level store: ONE agent conversation per project, alive for the whole
 * SPA session regardless of route — the omnipresence contract. React reads it
 * via subscribe/getSnapshot (useSyncExternalStore).
 */
export class PiStore {
  private _project: string;
  private agent: Agent;
  private model: Model<"openai-completions">;
  private cfg: PiConfig;
  private items: PiItem[] = [];
  private listeners = new Set<() => void>();
  private version = 0;
  busy = false;
  connected: boolean | null = null;
  lastError = "";

  private tools: AgentTool[];
  /** Guard against a model that answers every injection with more JSON. */
  private interceptBudget = 0;
  /** True while feeding a synthetic tool-result message back to the model. */
  private injecting = false;

  constructor(project: string) {
    this._project = project;
    this.cfg = loadPiConfig();
    this.model = installProvider(this.cfg);
    this.tools = buildTools(project);
    this.agent = new Agent({
      initialState: {
        systemPrompt: SYSTEM_PROMPT,
        model: this.model,
        thinkingLevel: "off",
        tools: this.tools,
      },
      streamFn: (m, c, o) => models.streamSimple(m, c, o),
      // Belt-and-braces for the keyless local provider (see installProvider).
      getApiKey: () => "local-keyless",
    });
    this.agent.subscribe((event) => this.onEvent(event));
    void this.probe();
  }

  get project(): string { return this._project; }

  /** True once anything is on screen (or a prompt is in flight) — i.e. there
   * is a conversation a store swap would orphan. */
  hasConversation(): boolean { return this.items.length > 0 || this.busy; }

  /**
   * Re-key this store to another project (first-paint promotion rescue —
   * see piStore below). The visible conversation is preserved; FUTURE tool
   * calls and memory writes target the new project. A tool call already in
   * flight keeps the project it was started under (its closure captured it),
   * which is correct: that is where the exchange actually began.
   */
  rekey(project: string) {
    this._project = project;
    this.tools = buildTools(project);
    this.agent.state.tools = this.tools;
    this.emit();
  }

  // -- external-store plumbing
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  getVersion = () => this.version;
  snapshot(): PiItem[] { return this.items; }
  get config(): PiConfig { return this.cfg; }
  private emit() {
    this.version++;
    this.items = [...this.items];
    for (const fn of this.listeners) fn();
  }

  /** Reachability probe for the model chip's status dot. */
  async probe() {
    try {
      const res = await fetch(`${this.cfg.baseUrl.replace(/\/$/, "")}/models`, { method: "GET" });
      this.connected = res.ok;
    } catch {
      this.connected = false;
    }
    this.emit();
  }

  setModel(modelId: string) {
    this.cfg = { ...this.cfg, modelId };
    savePiConfig(this.cfg);
    this.model = installProvider(this.cfg);
    this.agent.state.model = this.model;
    this.emit();
  }

  async send(text: string) {
    const prompt = text.trim();
    if (!prompt || this.busy) return;
    this.lastError = "";
    this.interceptBudget = 3;
    try {
      await this.agent.prompt(prompt);
    } catch (e) {
      this.lastError = e instanceof Error ? e.message : String(e);
      this.busy = false;
      this.emit();
    }
  }

  /** If the final assistant text IS a JSON tool call for a known tool,
   * swap the bubble for a tool row, run the tool, and continue the run
   * with the result. Returns true when an interception was started. */
  private tryInterceptTextToolCall(): boolean {
    if (this.interceptBudget <= 0) return false;
    const idx = this.items.length - 1;
    const last = this.items[idx];
    if (!last || last.kind !== "assistant" || !last.text) return false;
    const call = parseTextToolCall(last.text);
    if (!call) return false;
    const tool = this.tools.find((t) => t.name === call.name);
    if (!tool) return false;

    this.interceptBudget--;
    this.busy = true;
    const row: Extract<PiItem, { kind: "tool" }> = {
      kind: "tool", callId: `text-${Date.now()}`, name: call.name,
      label: call.name, args: call.args, status: "running",
    };
    this.items[idx] = row; // the raw-JSON bubble BECOMES the tool row
    this.emit();

    void (async () => {
      let resultText: string;
      try {
        const res = await tool.execute(row.callId, call.args);
        row.status = "ok";
        const details = (res as { details?: { result?: unknown; ms?: number } }).details;
        row.result = details?.result ?? res;
        row.ms = details?.ms;
        resultText = res.content
          .filter((c): c is { type: "text"; text: string } => c.type === "text")
          .map((c) => c.text).join("\n");
      } catch (e) {
        row.status = "error";
        row.result = e instanceof Error ? e.message : String(e);
        resultText = `ERROR: ${row.result}`;
      }
      this.emit();
      try {
        this.injecting = true; // hide the synthetic result message from the log
        await this.agent.prompt(
          `[${call.name} result]\n${resultText}\n\nAnswer the user's original question from this result, in plain prose. Do NOT output JSON.`,
        );
      } catch (e) {
        this.lastError = e instanceof Error ? e.message : String(e);
        this.busy = false;
        this.emit();
      }
    })();
    return true;
  }

  /** Explicit self-learning: distill the exchange into a memory_store write. */
  async learn(index: number) {
    const item = this.items[index];
    if (!item || item.kind !== "assistant" || item.learned || item.learning) return;
    const question = [...this.items.slice(0, index)].reverse()
      .find((i): i is Extract<PiItem, { kind: "user" }> => i.kind === "user")?.text ?? "";
    Object.assign(item, { learning: true });
    this.emit();
    try {
      const name = `pi-${question.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "exchange"}`;
      await api.post<PassthroughResponse>(
        `/api/agent/tool?project=${encodeURIComponent(this.project)}`,
        {
          name: "memory_store",
          args: {
            domain: "pi-agent",
            name,
            description: `Learned from a PI panel exchange (model ${this.cfg.modelId}).\nQ: ${question}\nA: ${item.text}`,
            type: "pattern",
            classification: "tactical",
          },
        },
      );
      Object.assign(item, { learning: false, learned: true });
      this.items.push({ kind: "note", text: `memory_store → stored (model ${this.cfg.modelId})` });
    } catch (e) {
      Object.assign(item, { learning: false });
      this.items.push({ kind: "note", text: `memory_store failed: ${e instanceof Error ? e.message : e}` });
    }
    this.emit();
  }

  toggleToolExpand() { /* expansion is component-local state; kept here for symmetry */ }

  // -- agent event fold
  private onEvent(event: Parameters<Parameters<Agent["subscribe"]>[0]>[0]) {
    switch (event.type) {
      case "agent_start":
        this.busy = true;
        break;
      case "agent_end": {
        this.busy = false;
        const err = this.agent.state.errorMessage;
        if (err) this.lastError = err;
        // Small-model resilience (senpi-style, the G5 reality): weak local
        // stacks emit the tool call as PLAIN JSON TEXT instead of a native
        // tool_call (seen live: Ollama 0.30.7 + qwen2.5-coder). Intercept
        // it, execute the tool app-side, feed the result back as a new turn.
        if (!err && this.tryInterceptTextToolCall()) return;
        this.interceptBudget = 0;
        const last = this.items[this.items.length - 1];
        if (last?.kind === "assistant") last.done = true;
        this.recordTelemetry(!err);
        break;
      }
      case "message_start": {
        const m = event.message as { role?: string };
        if (m.role === "user") {
          // Synthetic tool-result injections (text-tool-call fallback) are
          // context for the model, not conversation for the human.
          if (this.injecting) { this.injecting = false; break; }
          this.items.push({ kind: "user", text: messageText(event.message) });
        } else if (m.role === "assistant") {
          this.items.push({ kind: "assistant", text: "", done: false });
        }
        break;
      }
      case "message_update": {
        if ((event.message as { role?: string }).role !== "assistant") break;
        const last = [...this.items].reverse().find((i) => i.kind === "assistant");
        if (last && last.kind === "assistant") last.text = messageText(event.message);
        break;
      }
      case "message_end": {
        if ((event.message as { role?: string }).role !== "assistant") break;
        const last = [...this.items].reverse().find((i) => i.kind === "assistant");
        if (last && last.kind === "assistant") last.text = messageText(event.message);
        break;
      }
      case "tool_execution_start":
        this.items.push({
          kind: "tool", callId: event.toolCallId, name: event.toolName,
          label: event.toolName, args: event.args, status: "running",
        });
        break;
      case "tool_execution_end": {
        const row = this.items.find(
          (i): i is Extract<PiItem, { kind: "tool" }> => i.kind === "tool" && i.callId === event.toolCallId,
        );
        if (row) {
          row.status = event.isError ? "error" : "ok";
          const details = (event.result as { details?: { result?: unknown; ms?: number } })?.details;
          row.result = details?.result ?? event.result;
          row.ms = details?.ms;
        }
        break;
      }
      default:
        return; // turn_start/turn_end etc — no display impact
    }
    this.emit();
  }

  /** G5 feed: per-exchange model id + tool-loop success (localStorage ring). */
  private recordTelemetry(ok: boolean) {
    try {
      const key = "prism.pi.telemetry";
      const ring: PiTelemetry[] = JSON.parse(localStorage.getItem(key) ?? "[]");
      ring.push({ model: this.cfg.modelId, ok, at: Date.now() });
      localStorage.setItem(key, JSON.stringify(ring.slice(-50)));
    } catch { /* advisory only */ }
  }
}

/** Recognize an assistant text that IS a tool call — bare or fenced JSON
 * shaped {"name": "...", "arguments"|"args"|"parameters": {...}}. */
export function parseTextToolCall(text: string): { name: string; args: Record<string, unknown> } | null {
  let t = text.trim();
  const fence = /^```(?:json)?\s*([\s\S]*?)\s*```$/.exec(t);
  if (fence) t = fence[1].trim();
  if (!t.startsWith("{") || !t.endsWith("}")) return null;
  try {
    const obj = JSON.parse(t) as Record<string, unknown>;
    const name = obj.name;
    if (typeof name !== "string" || !name) return null;
    const args = obj.arguments ?? obj.args ?? obj.parameters ?? {};
    if (typeof args !== "object" || args === null || Array.isArray(args)) return null;
    return { name, args: args as Record<string, unknown> };
  } catch {
    return null;
  }
}

function messageText(message: unknown): string {
  const content = (message as { content?: unknown }).content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((p): p is { type: "text"; text: string } => (p as { type?: string }).type === "text")
      .map((p) => p.text)
      .join("");
  }
  return "";
}

const stores = new Map<string, PiStore>();

/** One-shot latch: only the FIRST default→real-project flip may rescue the
 * "default" store. Deliberate header-picker switches later in the session
 * always get their own per-project store. */
let defaultRescueSpent = false;

/**
 * The SPA's project identity settles AFTER first paint: `useProject` starts
 * from "default" and is promoted automatically (?project= deep-link mirror,
 * resolveInitialProject cold-start pick — see lib/project.ts). An exchange
 * started in that window lives in the "default"-keyed store; when the panel
 * re-renders under the promoted project it must NOT come up against a fresh
 * empty store with the running conversation orphaned. So the first time a
 * non-"default" project resolves, if the "default" store already holds a
 * conversation (and the new project has none), MIGRATE it: same store, same
 * items, re-keyed so future tool calls hit the promoted project.
 */
export function piStore(project: string): PiStore {
  let s = stores.get(project);
  if (!s) {
    s = rescueDefaultStore(project) ?? new PiStore(project);
    stores.set(project, s);
  }
  return s;
}

function rescueDefaultStore(project: string): PiStore | null {
  if (defaultRescueSpent || project === "default") return null;
  defaultRescueSpent = true; // consumed by the first real-project resolution
  const d = stores.get("default");
  if (!d || !d.hasConversation()) return null; // nothing to orphan
  stores.delete("default"); // move, don't alias — "default" gets a fresh store if ever revisited
  d.rekey(project);
  return d;
}
