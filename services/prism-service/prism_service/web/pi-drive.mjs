/**
 * pi-drive — shared drive-intent module for BOTH PI surfaces (task 4ada9ea0,
 * C5 of the PI-orchestration build, parent 81b23574 FR-5).
 *
 * A "plan/drive feature X" ask becomes exactly ONE POST /api/agent/drive
 * (C4, api/agent_drive.py) instead of N text-intercepted tool calls burning
 * the 6-call budget. This module owns ALL the logic — detection, the POST,
 * the human-readable summary — and the surfaces (web/pi-runtime.mjs, the
 * Node runner; web/src/lib/piAgent.ts, the SPA rail panel) only
 * detect-and-delegate. Single-source discipline matches pi-expert.mjs /
 * pi-toolcall.mjs. `node pi-drive.mjs --check` self-reports the wiring.
 */

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

// Conservative, explicit triggers ONLY — ordinary chat must never reroute.
// A drive intent starts the message with the verb: "plan <ask>", "drive
// <ask>", "plan: <ask>", "drive: <ask>". "planning"/"driven" do not match
// (the verb must be followed by whitespace or a colon).
const INTENT_RE = /^\s*(?:plan|drive)(?::\s*|\s+)(\S.*)/is;

/**
 * Detect an explicit plan/drive intent. Returns {task_id} when the ask
 * carries a task uuid (drive an EXISTING task), {ask} for a fresh feature
 * ask, or null for everything else (the normal chat/tool path).
 */
export function detectDriveIntent(text) {
  const m = INTENT_RE.exec(String(text ?? ""));
  if (!m) return null;
  const rest = m[1].trim();
  if (!rest) return null;
  const uuid = UUID_RE.exec(rest);
  if (uuid) return { task_id: uuid[0].toLowerCase() };
  return { ask: rest };
}

/**
 * Exactly ONE POST /api/agent/drive (the C4 contract, api/agent_drive.py):
 * body {ask?|task_id?, session_id?}, project rides the query param. Engine
 * refusals arrive as structured 200 {ok:false, reason} — surfaced, never
 * retried. Transport/HTTP failures are folded into the same shape so every
 * caller renders ONE result form.
 */
export async function postDrive({
  apiBase = "", project = "default", ask = "", taskId = "",
  sessionId = "", fetchFn,
} = {}) {
  const f = fetchFn ?? fetch;
  const base = String(apiBase ?? "").replace(/\/$/, "");
  const body = taskId ? { task_id: taskId } : { ask };
  if (sessionId) body.session_id = sessionId;
  try {
    const res = await f(
      `${base}/api/agent/drive?project=${encodeURIComponent(project)}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      return { ok: false, reason: `HTTP ${res.status}${detail ? `: ${detail.slice(0, 300)}` : ""}` };
    }
    return await res.json();
  } catch (err) {
    return { ok: false, reason: `drive endpoint unreachable: ${err?.message ?? err}` };
  }
}

/**
 * One compact human-readable receipt — the SAME text on both surfaces:
 * headline (final step + gate state, or the refusal reason verbatim),
 * task/run identity, per-step ok/ms lines, stats roll-up.
 */
export function formatDriveResult(r) {
  if (!r || typeof r !== "object") return "drive: no result";
  const lines = [];
  lines.push(r.ok
    ? `Drive complete — reached ${r.final_step || "?"} (gate ${r.gate_state || "?"}).`
    : `Drive stopped — ${r.reason || "unknown reason"}`);
  if (r.task_id) {
    lines.push(`task ${r.task_id}${r.created ? " (created)" : ""}${r.run_id ? ` · run ${r.run_id}` : ""}`);
  }
  for (const s of Array.isArray(r.steps) ? r.steps : []) {
    const outcome = s.kind === "gate"
      ? (s.gate_state || (s.ok ? "passed" : "failed"))
      : (s.to_step ? `→ ${s.to_step}` : (s.ok ? "ok" : "refused"));
    lines.push(`  ${s.ok ? "✓" : "✗"} ${s.step} [${s.kind}] ${outcome} (${Math.round(s.ms ?? 0)}ms)`);
  }
  const st = r.stats;
  if (st) {
    lines.push(`stats: ${st.advances ?? 0} advances · ${st.gates ?? 0} gates · `
      + `${st.model_calls ?? 0} model calls · ${st.overrides ?? 0} overrides`);
  }
  return lines.join("\n");
}

/**
 * Runner-shaped helper for web/pi-runtime.mjs: detect the intent on
 * job.prompt; when it IS a drive, do the one POST and return a result in
 * the runner's stdout contract ({ok, text, turns, tools_used, ms, tokens,
 * model}) — zero model construction, zero interception budget. Returns
 * null for non-drive jobs (the runner takes its existing path untouched).
 */
export async function maybeRunDrive(job) {
  const intent = detectDriveIntent(job?.prompt);
  if (!intent) return null;
  const started = Date.now();
  const result = await postDrive({
    apiBase: job.api_base,
    project: job.project || "default",
    ask: intent.ask ?? "",
    taskId: intent.task_id ?? "",
    sessionId: job.session_id || "",
  });
  const ms = Date.now() - started;
  return {
    ok: true, // the exchange completed; engine refusals are in the text
    text: formatDriveResult(result),
    turns: 0,
    tools_used: [{ name: "agent_drive", ms, ok: result?.ok === true }],
    ms, tokens: 0, model: "drive-engine", drive: result,
  };
}

// ------------------------------------------------------------- --check
// Offline self-report: detection matrix (positives + negatives), the
// formatter on a passed drive and a refusal, and whether BOTH surfaces
// import this module. Node-only (dynamic node: imports keep the module
// browser-safe for the Vite bundle).

async function runCheck() {
  const matrix = [
    ["plan feature whats-next card", "ask"],
    ["Plan: a whats-next card on the board", "ask"],
    ["drive task 4ada9ea0-a0b4-426e-973d-bf10c625a819", "task_id"],
    ["DRIVE 4ada9ea0-a0b4-426e-973d-bf10c625a819 through the SDLC", "task_id"],
    ["what tasks are open?", null],
    ["explain the plan_gate rubric", null],
    ["driven by telemetry", null],
    ["planning is hard", null],
  ];
  const detection = matrix.map(([text, want]) => {
    const got = detectDriveIntent(text);
    const kind = got ? (got.task_id ? "task_id" : "ask") : null;
    return { text, want, got: kind, pass: kind === want };
  });
  const detection_ok = detection.every((d) => d.pass);

  const passed = formatDriveResult({
    ok: true, task_id: "t-1", created: true, run_id: "r-1",
    final_step: "plan_gate", gate_state: "passed",
    stats: { advances: 5, authoring_steps: 2, model_calls: 1, overrides: 0, gates: 2, steps: 7 },
    steps: [{ step: "start", kind: "advance", ok: true, ms: 3, to_step: "review_previous_notes" },
            { step: "plan_gate", kind: "gate", ok: true, ms: 9, gate_state: "passed" }],
  });
  const refusal = formatDriveResult({ ok: false, reason: "plan_gate refused: no seeded principles" });
  const formatter_ok = passed.includes("plan_gate") && passed.includes("0 overrides")
    && refusal.includes("no seeded principles");

  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const { fileURLToPath } = await import("node:url");
  const here = path.dirname(fileURLToPath(import.meta.url));
  const readOr = (p) => fs.readFile(p, "utf-8").catch(() => "");
  const wired = {
    runtime: (await readOr(path.join(here, "pi-runtime.mjs"))).includes("pi-drive.mjs"),
    panel: (await readOr(path.join(here, "src", "lib", "piAgent.ts"))).includes("pi-drive.mjs"),
  };
  const ok = detection_ok && formatter_ok && wired.runtime && wired.panel;
  process.stdout.write(JSON.stringify({ ok, detection_ok, formatter_ok, wired, detection }));
  process.exitCode = ok ? 0 : 1;
}

// Fire ONLY when this file IS the entrypoint — an importer run with
// --check (node pi-runtime.mjs --check) must not double-print.
const isMain = typeof process !== "undefined" && Array.isArray(process.argv)
  && typeof process.argv[1] === "string"
  && /pi-drive\.mjs$/.test(process.argv[1].replace(/\\/g, "/"));
if (isMain && process.argv.includes("--check")) {
  runCheck().catch((err) => {
    process.stderr.write(String(err?.stack ?? err));
    process.exitCode = 1;
  });
}
