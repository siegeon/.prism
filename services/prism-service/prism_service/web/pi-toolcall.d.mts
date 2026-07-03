/**
 * Type surface for pi-toolcall.mjs (task c4bb21f8) — lets the SPA
 * (src/lib/piAgent.ts, tsc strict) import the shared multi-call parser
 * without duplicating it. Keep in sync with the runtime exports.
 */

/** One parsed text tool call: a catalog tool name + its argument object. */
export type ParsedToolCall = { name: string; args: Record<string, unknown> };

/** Parse an assistant text tool-call block into an ORDERED LIST of calls
 * (single object, top-level array, or concatenated objects). [] when the
 * text is not a JSON tool-call block. */
export declare function parseTextToolCalls(text: string): ParsedToolCall[];

/** Back-compat single-call contract: the FIRST parsed call, or null. */
export declare function parseTextToolCall(text: string): ParsedToolCall | null;

/** Strip small-model reasoning/tool markup (<think>, <workflow_state>, raw
 * tool-call JSON) from assistant text before rendering. "" when nothing
 * human-facing remains, so the UI can show a graceful fallback. */
export declare function sanitizeAssistantText(text: string): string;
