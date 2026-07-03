/**
 * Shared text-tool-call parser for PI's two interception surfaces
 * (task c4bb21f8 — the "PI panel dumps raw tool-call JSON" bug, dup
 * 4e5749bc). ONE source so a MULTI-call block parses identically on both
 * the Node runner (web/pi-runtime.mjs) and the browser panel
 * (web/src/lib/piAgent.ts) — same shared-module discipline as pi-expert.mjs.
 *
 * Small local models, asked to do several things in one turn (e.g. "search
 * the brain for X THEN create a task"), emit the WHOLE plan as plain JSON
 * TEXT instead of native tool_calls — as a single {name,arguments} object,
 * a top-level ARRAY of such objects, or several objects concatenated
 * (brace-adjacent `{..}{..}` or newline-separated). The single-object
 * interceptor executed nothing on the array/concatenated shapes, so the
 * raw JSON was shown and no tool ran.
 *
 * parseTextToolCalls returns an ORDERED LIST of {name,args}; the caller
 * validates each name against the LIVE tool catalog and executes
 * sequentially (unknown names skipped by the caller). parseTextToolCall
 * keeps the single-object contract for back-compat.
 */

/** Coerce one parsed JSON value into a {name,args} call, or null when it
 * is not a well-formed tool-call object (args must be a plain object). */
function normalizeCall(obj) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return null;
  const name = obj.name;
  if (typeof name !== "string" || !name) return null;
  const args = obj.arguments ?? obj.args ?? obj.parameters ?? {};
  if (typeof args !== "object" || args === null || Array.isArray(args)) return null;
  return { name, args };
}

/** Scan a string for every TOP-LEVEL JSON container ({...} or [...]),
 * brace-depth aware and string/escape aware so `{..}{..}`, `{..}\n{..}`,
 * and nested braces/strings all split correctly. Malformed chunks are
 * skipped, not thrown. */
function scanJsonValues(text) {
  const values = [];
  let depth = 0;
  let start = -1;
  let inStr = false;
  let esc = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === "\\") esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') { inStr = true; continue; }
    if (ch === "{" || ch === "[") {
      if (depth === 0) start = i;
      depth++;
    } else if (ch === "}" || ch === "]") {
      if (depth > 0) {
        depth--;
        if (depth === 0 && start !== -1) {
          try { values.push(JSON.parse(text.slice(start, i + 1))); } catch { /* skip */ }
          start = -1;
        }
      }
    }
  }
  return values;
}

/** Parse XML-form tool calls the way the SMALLEST models emit them, e.g.
 * `<task_list><status>pending</status></task_list>` instead of JSON. Each
 * top-level `<tool_name …>…</tool_name>` element becomes one {name,args}:
 * the tag is the tool name, root attributes and `<field>value</field>`
 * children become args. Returns an ORDERED LIST; [] when the text isn't an
 * XML element block. The caller still validates each name against the live
 * tool catalog, so non-tool XML simply never runs. */
export function parseXmlToolCalls(text) {
  const t = (text || "").trim();
  if (!t.startsWith("<")) return [];
  const calls = [];
  // top-level element: <tag attrs?> inner </tag>, tag = identifier.
  const elRe = /<([a-zA-Z_][\w-]*)((?:\s+[\w:-]+\s*=\s*"[^"]*")*)\s*>([\s\S]*?)<\/\1\s*>/g;
  let m;
  while ((m = elRe.exec(t)) !== null) {
    const name = m[1];
    const args = {};
    const attrRe = /([\w:-]+)\s*=\s*"([^"]*)"/g;
    let a;
    while ((a = attrRe.exec(m[2] || "")) !== null) args[a[1]] = a[2];
    const childRe = /<([a-zA-Z_][\w-]*)\s*>([\s\S]*?)<\/\1\s*>/g;
    let c;
    while ((c = childRe.exec(m[3] || "")) !== null) args[c[1]] = c[2].trim();
    calls.push({ name, args });
  }
  return calls;
}

/** Parse an assistant text that IS a tool-call block into an ORDERED LIST
 * of {name,args}. Handles: a bare or ```json-fenced single object; a
 * top-level array of objects; multiple concatenated objects; and XML-form
 * `<tool_name>…</tool_name>` blocks (the shape the smallest models emit).
 * Returns [] when the text is not a tool-call block (plain prose, etc.). */
export function parseTextToolCalls(text) {
  let t = (text || "").trim();
  const fence = /^```(?:json|xml)?\s*([\s\S]*?)\s*```$/.exec(t);
  if (fence) t = fence[1].trim();
  if (t.startsWith("<")) return parseXmlToolCalls(t);
  if (!t || !(t.startsWith("{") || t.startsWith("["))) return [];

  let candidates = null;
  try {
    // Fast path: the whole block is ONE JSON value (object or array).
    const parsed = JSON.parse(t);
    candidates = Array.isArray(parsed) ? parsed : [parsed];
  } catch {
    // Concatenated objects ({..}{..} / {..}\n{..}): only meaningful when
    // the block still closes a container — scan top-level JSON values.
    if (t.endsWith("}") || t.endsWith("]")) {
      candidates = scanJsonValues(t).flatMap((v) => (Array.isArray(v) ? v : [v]));
    }
  }
  if (!candidates) return [];

  const calls = [];
  for (const c of candidates) {
    const call = normalizeCall(c);
    if (call) calls.push(call);
  }
  return calls;
}

/** Back-compat single-call contract: the FIRST parsed tool call, or null. */
export function parseTextToolCall(text) {
  const calls = parseTextToolCalls(text);
  return calls.length ? calls[0] : null;
}


/** Small local models often wrap their reply in pseudo-markup the UI must
 * not show raw: <think>…</think> reasoning, <workflow_state>{…}</workflow_state>
 * / <tool_call>…</tool_call> fake tags, or a whole-message tool-call JSON blob
 * the interceptor already executed. Strip those so the customer sees a clean
 * answer. Returns "" when nothing readable remains (caller shows a fallback). */
export function sanitizeAssistantText(text) {
  if (!text) return "";
  let t = String(text);
  // paired pseudo-XML blocks (thinking / fake state / tool tags)
  t = t.replace(
    /<(think|thinking|reasoning|scratchpad|plan|workflow_state|tool_call|tool_calls|function_call|tool_result)\b[^>]*>[\s\S]*?<\/\1>/gi,
    "");
  // any other <xxx_state>…</xxx_state> pseudo tag
  t = t.replace(/<([a-z][a-z0-9_]*_state)\b[^>]*>[\s\S]*?<\/\1>/gi, "");
  // XML-form tool calls the interceptor couldn't run (unknown tool / budget
  // spent): <task_list>…</task_list> etc. The snake_case-WITH-underscore tag
  // is always model scaffolding here (real HTML tags have no underscore), so
  // strip the whole block rather than render raw markup at the customer.
  t = t.replace(/<([a-z][a-z0-9]*_[a-z0-9_]*)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "");
  // an unterminated leading think block ("<think> …" with no close)
  t = t.replace(/<(think|thinking|reasoning)\b[^>]*>[\s\S]*$/i, "");
  // a whole-message JSON tool-call blob that survived interception
  const trimmed = t.trim();
  if (/^[[{][\s\S]*[}\]]$/.test(trimmed) && parseTextToolCalls(trimmed).length) {
    return "";
  }
  return t.trim();
}
