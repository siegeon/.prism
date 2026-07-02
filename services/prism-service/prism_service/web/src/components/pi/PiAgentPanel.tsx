/**
 * PI agent chat panel — the omnipresent left-rail assistant (task 711d5235).
 *
 * Renders the pi-agent-core event stream folded by lib/piAgent.ts: user and
 * assistant bubbles (Markdown bodies), one-line collapsed tool-call rows that
 * click-expand to per-tool renderers (progressive disclosure — never a wall
 * of text), a model status chip + picker, per-exchange token stats, and the
 * explicit "learn this" self-learning affordance. Inference is local-only;
 * the footer says so.
 *
 * Rendering architecture — structure borrowed from @earendil-works/pi-web-ui
 * (MIT, Mario Zechner), reimplemented for React/Hermes: settled transcript
 * items live in a memoized list that only re-renders on structural changes
 * (item count / a settled item), while the in-flight assistant text streams
 * through an isolated component on the store's dedicated stream channel —
 * no full-transcript re-render per token.
 *
 * pi toolkit: https://github.com/badlogic/pi-mono (MIT, Mario Zechner).
 */
import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { ChevronDown, ChevronRight, Hexagon, Send, Sparkles, Square } from "lucide-react";
import Markdown from "@/components/Markdown";
import { TOOL_RENDERERS, prettyReceipt } from "@/components/pi/toolRenderers";
import { useProject } from "@/lib/project";
import { cn } from "@/lib/utils";
import {
  PI_MODEL_CHOICES,
  piStore,
  type PiItem,
  type PiStore,
} from "@/lib/piAgent";

const SUGGESTED = ["What tasks are pending?", "What is in this project's brain?"];

export default function PiAgentPanel() {
  const [project] = useProject();
  const store = piStore(project);
  useSyncExternalStore(store.subscribe, store.getVersion, store.getVersion);
  const items = store.snapshot();

  const [pickerOpen, setPickerOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Stable/streaming split: while an assistant message is in flight it is
  // rendered by <StreamingBubble> off the stream channel; everything before
  // it is the settled, memoized transcript.
  const tail = items[items.length - 1];
  const streaming = store.busy && tail?.kind === "assistant" && !tail.done;
  const settled = useMemo(
    () => (streaming ? items.slice(0, -1) : items),
    [items, streaming],
  );

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items, store.busy]);

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <PanelHeader store={store} pickerOpen={pickerOpen} setPickerOpen={setPickerOpen} />

      <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto px-3 py-3 space-y-2">
        {items.length === 0 && (
          <div className="text-xs text-[color:var(--text-muted)] leading-relaxed px-1 space-y-3">
            <p>
              Answers come from this project's real data — brain, memory,
              tasks — via self-executed PRISM tool calls on a small local model.
            </p>
            <div className="flex flex-col gap-1.5">
              {SUGGESTED.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => { if (!store.busy) void store.send(s); }}
                  className="text-left px-2.5 py-1.5 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] text-[color:var(--text-secondary)] hover:text-[color:var(--text-primary)] hover:bg-[color:var(--surface-3)] transition-colors text-xs"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        <SettledList items={settled} store={store} />
        {streaming && <StreamingBubble store={store} />}
        {store.busy && !streaming && (
          <div className="px-2 py-1 text-xs text-[color:var(--text-muted)] animate-pulse">thinking…</div>
        )}
        {store.lastError && (
          <div className="mx-1 px-2.5 py-2 rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] text-xs break-words">
            {store.lastError}
          </div>
        )}
      </div>

      <Composer store={store} busy={store.busy} />
    </div>
  );
}

/**
 * Settled transcript — memoized so it re-renders ONLY when the settled items
 * array is replaced (the store recreates it exclusively on structural emits:
 * item added, tool status change, exchange finished). Token-level streaming
 * never touches this list.
 */
const SettledList = memo(function SettledList({
  items, store,
}: {
  items: PiItem[];
  store: PiStore;
}) {
  return (
    <>
      {items.map((item, i) => (
        <PiItemRow key={i} item={item} index={i} store={store} />
      ))}
    </>
  );
});

/**
 * In-flight assistant text, isolated on the store's stream channel: each
 * message_update fold bumps only the stream version, so this bubble is the
 * ONLY component that re-renders per token.
 */
function StreamingBubble({ store }: { store: PiStore }) {
  useSyncExternalStore(store.subscribeStream, store.getStreamVersion, store.getStreamVersion);
  const text = store.streamingText;
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { ref.current?.scrollIntoView({ block: "nearest" }); }, [text]);
  return (
    <div ref={ref} className="max-w-[96%] px-3 py-2 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-subtle)]">
      {text
        ? <Markdown text={text} className="space-y-2" />
        : <span className="text-xs text-[color:var(--text-muted)] animate-pulse">…</span>}
    </div>
  );
}

/** True when the browser auto-grows the textarea natively. */
const FIELD_SIZING_SUPPORTED =
  typeof CSS !== "undefined" && typeof CSS.supports === "function" &&
  CSS.supports("field-sizing", "content");

/**
 * Input editor (pi-web-ui behavior): auto-growing textarea — CSS
 * `field-sizing: content` where supported, scrollHeight JS fallback where
 * not — Enter sends, Shift+Enter inserts a newline, and the send button
 * becomes a stop button (agent.abort()) while a run is in flight. Draft
 * state is local so typing never re-renders the transcript.
 */
function Composer({ store, busy }: { store: PiStore; busy: boolean }) {
  const [draft, setDraft] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (FIELD_SIZING_SUPPORTED) taRef.current?.style.setProperty("field-sizing", "content");
  }, []);

  const autoGrow = () => {
    const el = taRef.current;
    if (!el || FIELD_SIZING_SUPPORTED) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 128)}px`;
  };

  const send = () => {
    const text = draft.trim();
    if (!text || busy) return;
    setDraft("");
    const el = taRef.current;
    if (el && !FIELD_SIZING_SUPPORTED) el.style.height = "auto";
    void store.send(text);
  };

  return (
    <div className="border-t border-[color:var(--border-default)] p-2.5 space-y-1.5">
      <form
        className="flex items-end gap-1.5"
        onSubmit={(e) => { e.preventDefault(); send(); }}
      >
        <textarea
          ref={taRef}
          rows={1}
          value={draft}
          onChange={(e) => { setDraft(e.target.value); autoGrow(); }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
          }}
          placeholder="Ask about this project…"
          className="flex-1 min-w-0 px-2.5 py-1.5 rounded-md text-sm resize-none max-h-32 overflow-y-auto bg-[color:var(--surface-0)] border border-[color:var(--border-default)] text-[color:var(--text-primary)] placeholder:text-[color:var(--text-disabled)] focus:outline-none focus:border-[color:var(--border-strong)]"
        />
        {busy ? (
          <button
            type="button"
            onClick={() => store.abort()}
            title="Stop"
            className="p-2 rounded-md border border-[color:var(--accent-rose-ring)] text-[color:var(--accent-rose-fg)] hover:bg-[color:var(--accent-rose-bg)] transition-colors"
          >
            <Square className="w-3.5 h-3.5" />
          </button>
        ) : (
          <button
            type="submit"
            disabled={!draft.trim()}
            title="Send"
            className="p-2 rounded-md border border-[color:var(--border-default)] text-[color:var(--text-secondary)] hover:text-[color:var(--text-primary)] hover:bg-[color:var(--surface-2)] disabled:opacity-40 disabled:pointer-events-none transition-colors"
          >
            <Send className="w-3.5 h-3.5" />
          </button>
        )}
      </form>
      <div className="px-0.5 text-[10px] uppercase tracking-wider text-[color:var(--text-disabled)]">
        inference: local · 0 tokens sent to cloud
      </div>
    </div>
  );
}

function PanelHeader({
  store, pickerOpen, setPickerOpen,
}: {
  store: PiStore;
  pickerOpen: boolean;
  setPickerOpen: (v: boolean) => void;
}) {
  const cfg = store.config;
  const experimental = PI_MODEL_CHOICES.find((m) => m.id === cfg.modelId)?.experimental;
  return (
    <div className="relative border-b border-[color:var(--border-default)]">
      {/* Model selector — a full-width bar directly under the rail's tab
          toggle: reads as a second tab row, opens the picker. */}
      <button
        type="button"
        onClick={() => { setPickerOpen(!pickerOpen); void store.probe(); }}
        title={`Local model endpoint: ${cfg.baseUrl}`}
        className="w-full flex items-center gap-2 px-4 py-2 text-[11px] text-[color:var(--accent-teal-fg)] hover:bg-[color:var(--accent-teal-bg)] transition-colors"
      >
        <Hexagon className="w-3 h-3 shrink-0" />
        <span className="font-mono truncate">{cfg.modelId}</span>
        <span
          className={cn(
            "ml-auto w-1.5 h-1.5 rounded-full shrink-0",
            store.connected === true && "bg-[color:var(--accent-emerald-fg)] animate-pulse",
            store.connected === false && "bg-[color:var(--accent-rose-fg)]",
            store.connected === null && "bg-[color:var(--text-disabled)]",
          )}
        />
      </button>
      {experimental && (
        <div className="px-4 pb-1.5 text-[10px] text-[color:var(--accent-amber-fg)]">
          G5: probing smaller models — tool loops may fail
        </div>
      )}
      {pickerOpen && (
        <div className="absolute left-2 right-2 top-full mt-1 z-20 rounded-md border border-[color:var(--border-strong)] bg-[color:var(--surface-3)] shadow-xl py-1">
          {PI_MODEL_CHOICES.map((m) => (
            <button
              key={m.id}
              type="button"
              onClick={() => { store.setModel(m.id); setPickerOpen(false); }}
              className={cn(
                "w-full text-left px-3 py-1.5 text-xs transition-colors hover:bg-[color:var(--surface-2)]",
                m.id === cfg.modelId
                  ? "text-[color:var(--text-primary)]"
                  : "text-[color:var(--text-secondary)]",
              )}
            >
              <span className="font-mono">{m.label}</span>
              {m.hint && (
                <span className={cn(
                  "ml-2 text-[10px]",
                  m.experimental
                    ? "text-[color:var(--accent-amber-fg)]"
                    : "text-[color:var(--text-muted)]",
                )}>
                  {m.hint}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function PiItemRow({ item, index, store }: { item: PiItem; index: number; store: PiStore }) {
  if (item.kind === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[92%] px-3 py-2 rounded-md bg-[color:var(--accent-violet-bg)] ring-1 ring-inset ring-[color:var(--accent-violet-ring)] text-sm text-[color:var(--text-primary)] whitespace-pre-wrap break-words">
          {item.text}
        </div>
      </div>
    );
  }
  if (item.kind === "assistant") {
    return (
      <div className="space-y-1">
        <div className="max-w-[96%] px-3 py-2 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-subtle)]">
          {item.text
            ? <Markdown text={item.text} className="space-y-2" />
            : <span className="text-xs text-[color:var(--text-muted)] animate-pulse">…</span>}
        </div>
        {item.done && item.text && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={item.learned || item.learning}
              onClick={() => void store.learn(index)}
              className={cn(
                "flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wider transition-colors",
                item.learned
                  ? "bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] ring-1 ring-inset ring-[color:var(--accent-emerald-ring)]"
                  : "text-[color:var(--text-muted)] hover:text-[color:var(--text-secondary)] hover:bg-[color:var(--surface-2)]",
              )}
            >
              <Sparkles className="w-3 h-3" />
              {item.learned ? "in memory" : item.learning ? "storing…" : "learn this"}
            </button>
            {item.stats && (
              <span
                className="text-[10px] font-mono text-[color:var(--text-muted)]"
                title={item.stats.estimated ? "estimated (text length / 4 — no usage in stream)" : "reported by the model stream"}
              >
                ↑{item.stats.input} ↓{item.stats.output} tok · {item.stats.seconds.toFixed(1)}s
                {item.stats.estimated ? " ~" : ""}
              </span>
            )}
          </div>
        )}
      </div>
    );
  }
  if (item.kind === "tool") return <ToolRow item={item} />;
  return (
    <div className="px-2 text-[10px] text-[color:var(--text-muted)] font-mono">{item.text}</div>
  );
}

/** One-line collapsed tool receipt; click to expand (progressive disclosure).
 * The expanded body goes through the per-tool renderer registry
 * (toolRenderers.tsx); unknown tools / unexpected shapes fall back to the
 * raw mono receipt. */
function ToolRow({ item }: { item: Extract<PiItem, { kind: "tool" }> }) {
  const [open, setOpen] = useState(false);
  const argSummary = summarize(item.args);
  const custom = open && item.status === "ok"
    ? TOOL_RENDERERS.get(item.name)?.(item.result) ?? null
    : null;
  return (
    <div className="mx-1">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className={cn(
          "w-full flex items-center gap-1.5 px-2 py-1 rounded-md text-[11px] font-mono transition-colors text-left",
          item.status === "error"
            ? "text-[color:var(--accent-rose-fg)] bg-[color:var(--accent-rose-bg)]"
            : "text-[color:var(--accent-teal-fg)] hover:bg-[color:var(--accent-teal-bg)]",
          item.status === "running" && "animate-pulse",
        )}
      >
        {open ? <ChevronDown className="w-3 h-3 shrink-0" /> : <ChevronRight className="w-3 h-3 shrink-0" />}
        <span className="truncate">
          {item.name}
          {argSummary && <span className="text-[color:var(--text-muted)]"> · {argSummary}</span>}
        </span>
        <span className="ml-auto shrink-0 text-[color:var(--text-muted)]">
          {item.status === "running" ? "running…" : item.ms != null ? `${item.ms}ms` : item.status}
        </span>
      </button>
      {open && item.status !== "running" && (
        custom != null ? (
          <div className="mt-1 mb-1 mx-1 rounded-md bg-[color:var(--surface-1)] border border-[color:var(--border-subtle)] p-1 max-h-64 overflow-y-auto">
            {custom}
          </div>
        ) : (
          <div className="mt-1 mb-1 mx-1 px-2.5 py-2 rounded-md bg-[color:var(--surface-0)] border border-[color:var(--border-default)] max-h-48 overflow-y-auto">
            <div className="text-[10px] leading-relaxed font-mono text-[color:var(--text-secondary)] whitespace-pre-wrap break-words">
              {prettyReceipt(item.result)}
            </div>
          </div>
        )
      )}
    </div>
  );
}

function summarize(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const vals = Object.values(args as Record<string, unknown>).filter(
    (v): v is string => typeof v === "string" && v.length > 0,
  );
  const s = vals.join(", ");
  return s.length > 40 ? `${s.slice(0, 40)}…` : s;
}
