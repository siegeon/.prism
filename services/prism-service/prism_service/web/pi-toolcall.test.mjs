/**
 * Node unit check for the shared text-tool-call parser (task c4bb21f8).
 * Exercises BOTH interception surfaces' parse logic at once, since
 * pi-runtime.mjs and src/lib/piAgent.ts both import pi-toolcall.mjs.
 *
 * Run standalone: `node pi-toolcall.test.mjs` (exit 0 = pass, 1 = fail).
 * Driven by tests/integration/test_pi_multicall_interception.py.
 */

import { parseTextToolCall, parseTextToolCalls } from "./pi-toolcall.mjs";

let failures = 0;
function check(label, cond) {
  if (!cond) { failures++; process.stderr.write(`FAIL: ${label}\n`); }
}

// (1) Single object — the shape that already worked — still parses to one.
const single = parseTextToolCalls('{"name": "brain_search", "arguments": {"query": "x"}}');
check("single -> 1 call", single.length === 1);
check("single name", single[0]?.name === "brain_search");
check("single args", single[0]?.args?.query === "x");

// (2) Fenced single object still parses.
const fenced = parseTextToolCalls('```json\n{"name": "memory_recall", "args": {"q": "y"}}\n```');
check("fenced -> 1 call", fenced.length === 1);
check("fenced name", fenced[0]?.name === "memory_recall");

// (3) Top-level ARRAY of tool calls -> ordered list (THE BUG).
const arr = parseTextToolCalls(
  '[{"name":"brain_search","arguments":{"query":"auth"}},' +
  '{"name":"task_create","arguments":{"title":"Fix auth"}}]',
);
check("array -> 2 calls", arr.length === 2);
check("array order[0]", arr[0]?.name === "brain_search");
check("array order[1]", arr[1]?.name === "task_create");
check("array args carried", arr[1]?.args?.title === "Fix auth");

// (4) Concatenated objects, newline-separated -> ordered list.
const catNl = parseTextToolCalls(
  '{"name":"brain_search","arguments":{"query":"auth"}}\n' +
  '{"name":"task_create","arguments":{"title":"Fix auth"}}',
);
check("concat(newline) -> 2 calls", catNl.length === 2);
check("concat(newline) order", catNl[0]?.name === "brain_search" && catNl[1]?.name === "task_create");

// (5) Concatenated objects, brace-adjacent -> ordered list.
const catAdj = parseTextToolCalls(
  '{"name":"brain_search","arguments":{"query":"a"}}{"name":"memory_recall","arguments":{"q":"b"}}',
);
check("concat(adjacent) -> 2 calls", catAdj.length === 2);
check("concat(adjacent) order", catAdj[0]?.name === "brain_search" && catAdj[1]?.name === "memory_recall");

// (6) Mixed valid + malformed element: keep the valid one, skip the junk.
const mixed = parseTextToolCalls(
  '[{"name":"brain_search","arguments":{"query":"a"}},{"noname":true},{"name":"task_create","arguments":{"title":"t"}}]',
);
check("mixed -> 2 valid calls", mixed.length === 2);
check("mixed keeps names", mixed[0]?.name === "brain_search" && mixed[1]?.name === "task_create");

// (7) Non-tool-call prose -> no calls (no false interception).
check("prose -> 0", parseTextToolCalls("Sure, I searched the brain and found nothing.").length === 0);
check("empty -> 0", parseTextToolCalls("").length === 0);

// (8) Back-compat single accessor: first call, or null.
check("parseTextToolCall single", parseTextToolCall('{"name":"brain_search","arguments":{}}')?.name === "brain_search");
check("parseTextToolCall array-first", parseTextToolCall(
  '[{"name":"first","arguments":{}},{"name":"second","arguments":{}}]')?.name === "first");
check("parseTextToolCall prose null", parseTextToolCall("hello") === null);

if (failures) {
  process.stderr.write(`pi-toolcall.test.mjs: ${failures} assertion(s) failed\n`);
  process.exit(1);
}
process.stdout.write("pi-toolcall.test.mjs: all checks passed\n");
