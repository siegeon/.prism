/**
 * Agent bridge — live remote-assist (plan: peaceful-seeking-octopus).
 *
 * An authorized agent, holding a user's OWN PRISM access key, can drive that
 * SAME user's already-open tab: navigate/click/fill/read, live, on their own
 * screen. No separate/headless browser, no screen mirror — this provider is
 * a small ALWAYS-MOUNTED piece of the real app (mounted once in App.tsx,
 * outside <Routes>, so it survives navigation) that opens one command
 * channel and executes commands against the REAL DOM/router.
 *
 * Consent lives entirely on this side: nothing here is reachable by an agent
 * until the signed-in user calls `enable()` (wired to the Settings toggle,
 * next to the existing access-key panel — same trust model). The session
 * token this mints is a distinct, narrow credential from the user's general
 * access key, and is mirrored into `sessionStorage` (never `localStorage` —
 * that would let it outlive the tab entirely, which is a real security
 * regression, not just a UX one). `sessionStorage` is the correct middle
 * ground: it survives a same-tab reload (React state alone does not — a
 * plain F5 wiped `session` back to null even though the user never touched
 * the toggle, which the owner read as "the feature turned itself off"), but
 * it IS cleared automatically the moment the tab/window actually closes, so
 * "must not outlive the tab" still holds. See v7.13.4's changelog entry.
 *
 * Command channel: reuses this app's existing live-push infrastructure
 * (lib/sharedStream.ts's subscribeStream — the ref-counted single-
 * EventSource-per-URL abstraction every other SSE consumer already shares)
 * against the new `GET /sse/agent-bridge/{id}` route. Results flow back via
 * an ordinary POST, authenticated with the bridge token itself (not the
 * general access key — see api/agent_bridge.py).
 */

import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
  type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import { subscribeStream } from "@/lib/sharedStream";
import { api } from "@/lib/api";
import { getProject } from "@/lib/project";

type BridgeSession = {
  id: string;
  token: string;
  project: string;
  expires_at: number;
};

type BridgeCommand = {
  type: "agent_bridge.command";
  session_id: string;
  command_id: string;
  action:
    | "navigate" | "click" | "fill" | "read" | "screenshot"
    // Observability (v7.14): console/network have been recording into a
    // bounded ring buffer since this module loaded, not just from the
    // moment they're first called -- see installObservability() below.
    | "console" | "network"
    // Interaction parity with a real browser-automation toolset.
    | "hover" | "drag" | "select_option" | "file_upload" | "press_key"
    | "handle_dialog" | "wait_for" | "tabs" | "navigate_back" | "find";
  path?: string;
  selector?: string;
  value?: string;
  // drag: the drop target (source is `selector`).
  target_selector?: string;
  // file_upload: files to place on an <input type="file">.
  files?: Array<{ name: string; type?: string; content_base64: string }>;
  // handle_dialog: pre-arm the next native confirm()/alert()/prompt() to
  // resolve non-blocking with this decision (see installDialogOverride).
  accept?: boolean;
  // console/network: cap how many ring-buffer entries to return.
  limit?: number;
  // wait_for: how long to poll before giving up (real polling, not a
  // fixed sleep) -- must stay comfortably under the MCP tool's own
  // COMMAND_TIMEOUT_SECONDS or the SERVER times out first with a less
  // useful "the browser never responded" error.
  timeout_ms?: number;
  // find: locate by ARIA role and/or accessible-name / text substring,
  // optionally scoped under `selector`.
  role?: string;
  name?: string;
  text?: string;
};

type AgentBridgeState = {
  session: BridgeSession | null;
  enabling: boolean;
  error: string | null;
  enable: () => Promise<void>;
  disable: () => Promise<void>;
};

const AgentBridgeContext = createContext<AgentBridgeState | null>(null);

// sessionStorage (NOT localStorage, see this file's header docstring) is the
// only durable copy of a bridge session — it lets a same-tab reload resume
// transparently instead of silently forgetting a session the user never
// asked to end, while still dying with the tab itself.
const STORAGE_KEY = "prism.agentBridgeSession";

function loadPersistedSession(): BridgeSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<BridgeSession>;
    if (typeof parsed?.id !== "string" || typeof parsed?.token !== "string") {
      return null; // malformed/stale entry — never hydrate a half session
    }
    return parsed as BridgeSession;
  } catch {
    return null;
  }
}

function persistSession(s: BridgeSession | null): void {
  try {
    if (s) {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(s));
    } else {
      sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // sessionStorage can throw (private-mode quirks, storage disabled) —
    // persistence is a convenience for the NEXT reload, never a hard
    // requirement for the bridge to work this tab-load.
  }
}

/** Read from any component (e.g. the Settings toggle) to show/drive state. */
export function useAgentBridge(): AgentBridgeState {
  const ctx = useContext(AgentBridgeContext);
  if (!ctx) {
    throw new Error("useAgentBridge must be used within AgentBridgeProvider");
  }
  return ctx;
}

function resolveSelector(selector: string): Element | null {
  try {
    return document.querySelector(selector);
  } catch {
    return null; // an invalid selector string must fail the command, not throw
  }
}

/** Set an <input>/<textarea>/<select>'s value through React's OWN tracked
 * setter, not the plain DOM property — a plain assignment is invisible to a
 * controlled component's onChange, because React wraps the native setter
 * to notice writes. Dispatching real 'input'/'change' events afterward is
 * what makes this indistinguishable from the person typing/choosing. A
 * <select> has no native OS dropdown to click through here (that's a
 * browser-chrome popup, not a DOM node) — setting .value + firing 'change'
 * is the standard, correct way to drive one programmatically; it is NOT a
 * fallback or a simulation, it's what onChange actually listens for. */
function setNativeValue(
  el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string,
): void {
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : el instanceof HTMLSelectElement
    ? HTMLSelectElement.prototype
    : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

// ---------------------------------------------------------------------------
// Observability: console + network ring buffers.
//
// These install at MODULE LOAD, not at enable()/mount time, and unconditionally
// (not gated on a bridge session existing) — a driver that enables Remote
// Assist, navigates, and THEN calls the `console` action must still see
// whatever fired during that navigation, and this app's own client-side
// routing never reloads the page, so a buffer that only started recording
// when `console` is first CALLED would already have missed it. Capturing
// unconditionally costs a few wrapped function calls; nothing here is ever
// exposed except through an already-authorized bridge session.
// ---------------------------------------------------------------------------

type ConsoleEntry = { level: "log" | "warn" | "error"; message: string; ts: number };
type NetworkEntry = {
  method: string; url: string; status: number; ok: boolean;
  duration_ms: number; ts: number; error?: string;
};
type DialogEntry = {
  kind: "alert" | "confirm" | "prompt"; message: string;
  accepted: boolean; text?: string; ts: number;
};

const RING_LIMIT = 300;
const consoleLog: ConsoleEntry[] = [];
const networkLog: NetworkEntry[] = [];
const dialogLog: DialogEntry[] = [];

function pushRing<T>(ring: T[], entry: T): void {
  ring.push(entry);
  if (ring.length > RING_LIMIT) ring.splice(0, ring.length - RING_LIMIT);
}

function stringifyArg(a: unknown): string {
  if (typeof a === "string") return a;
  try {
    return JSON.stringify(a);
  } catch {
    return String(a);
  }
}

let _observabilityInstalled = false;

/** Patches console.*, window.onerror/unhandledrejection, fetch, and
 * XMLHttpRequest so the `console`/`network` actions have real history to
 * report the instant they're first called, not just going forward. */
function installObservability(): void {
  if (_observabilityInstalled) return;
  _observabilityInstalled = true;

  const consoleAny = console as unknown as Record<string, (...a: unknown[]) => void>;
  (["log", "info", "warn", "error", "debug"] as const).forEach((method) => {
    const orig = consoleAny[method];
    if (typeof orig !== "function") return;
    const level: ConsoleEntry["level"] =
      method === "warn" ? "warn" : method === "error" ? "error" : "log";
    consoleAny[method] = (...args: unknown[]) => {
      try {
        pushRing(consoleLog, { level, message: args.map(stringifyArg).join(" "), ts: Date.now() });
      } catch {
        // capture must never be why the app's own logging breaks
      }
      orig.apply(console, args);
    };
  });

  window.addEventListener("error", (ev) => {
    pushRing(consoleLog, {
      level: "error",
      message: `${ev.message || "script error"} (${ev.filename ?? "?"}:${ev.lineno ?? "?"})`,
      ts: Date.now(),
    });
  });
  window.addEventListener("unhandledrejection", (ev) => {
    const reason = ev.reason as { message?: string } | undefined;
    pushRing(consoleLog, {
      level: "error",
      message: `unhandled rejection: ${String(reason?.message ?? ev.reason)}`,
      ts: Date.now(),
    });
  });

  const origFetch = window.fetch.bind(window);
  window.fetch = (async (...args: Parameters<typeof fetch>) => {
    const start = performance.now();
    const first = args[0];
    const url = typeof first === "string" ? first
      : first instanceof URL ? first.toString()
      : (first as Request).url;
    const method = (
      (args[1] as RequestInit | undefined)?.method
      ?? (first instanceof Request ? first.method : undefined)
      ?? "GET"
    ).toUpperCase();
    try {
      const res = await origFetch(...args);
      pushRing(networkLog, {
        method, url, status: res.status, ok: res.ok,
        duration_ms: Math.round(performance.now() - start), ts: Date.now(),
      });
      return res;
    } catch (e) {
      pushRing(networkLog, {
        method, url, status: 0, ok: false,
        duration_ms: Math.round(performance.now() - start), ts: Date.now(),
        error: String((e as Error)?.message ?? e),
      });
      throw e;
    }
  }) as typeof fetch;

  // Some consumers (this app's own SSE polyfill fallbacks aside) still use
  // XMLHttpRequest directly -- cover it too so `network` isn't fetch-only.
  const XHRProto = XMLHttpRequest.prototype as XMLHttpRequest & {
    __bridgeMethod?: string; __bridgeUrl?: string;
  };
  const origOpen = XHRProto.open;
  const origSend = XHRProto.send;
  XHRProto.open = function (
    this: XMLHttpRequest & { __bridgeMethod?: string; __bridgeUrl?: string },
    method: string, url: string | URL, ...rest: unknown[]
  ) {
    this.__bridgeMethod = String(method || "GET").toUpperCase();
    this.__bridgeUrl = String(url);
    return (origOpen as (...a: unknown[]) => void).apply(this, [method, url, ...rest]);
  };
  XHRProto.send = function (
    this: XMLHttpRequest & { __bridgeMethod?: string; __bridgeUrl?: string },
    ...args: unknown[]
  ) {
    const start = performance.now();
    this.addEventListener("loadend", () => {
      pushRing(networkLog, {
        method: this.__bridgeMethod ?? "GET",
        url: this.__bridgeUrl ?? "",
        status: this.status,
        ok: this.status >= 200 && this.status < 400,
        duration_ms: Math.round(performance.now() - start),
        ts: Date.now(),
      });
    });
    return (origSend as (...a: unknown[]) => void).apply(this, args);
  };
}
installObservability();

// ---------------------------------------------------------------------------
// Native dialogs: window.confirm/alert/prompt BLOCK the JS thread until
// answered, which would freeze this exact bridge (no further SSE command
// could ever be delivered/executed while blocked -- the tab that's supposed
// to keep listening is the same tab that just froze). So the override never
// lets one actually block: it always resolves immediately, either from a
// policy the `handle_dialog` command pre-armed (call it BEFORE the action
// that triggers the dialog) or from a safe default (accept/confirm truthy,
// empty string for prompt) — and always records what happened so a driver
// can inspect it afterward via `handle_dialog` with no args, or `console`.
// ---------------------------------------------------------------------------

type DialogPolicy = { accept: boolean; text?: string };
let _dialogPolicy: DialogPolicy | null = null;
let _dialogInstalled = false;

function installDialogOverride(): void {
  if (_dialogInstalled) return;
  _dialogInstalled = true;

  window.alert = (message?: unknown) => {
    pushRing(dialogLog, { kind: "alert", message: String(message ?? ""), accepted: true, ts: Date.now() });
    _dialogPolicy = null;
  };
  window.confirm = (message?: unknown) => {
    const policy = _dialogPolicy ?? { accept: true };
    _dialogPolicy = null;
    pushRing(dialogLog, {
      kind: "confirm", message: String(message ?? ""), accepted: policy.accept, ts: Date.now(),
    });
    return policy.accept;
  };
  window.prompt = (message?: unknown, _default?: string) => {
    const policy = _dialogPolicy ?? { accept: true, text: _default ?? "" };
    _dialogPolicy = null;
    pushRing(dialogLog, {
      kind: "prompt", message: String(message ?? ""), accepted: policy.accept,
      text: policy.text ?? "", ts: Date.now(),
    });
    return policy.accept ? (policy.text ?? "") : null;
  };
}
installDialogOverride();

// ---------------------------------------------------------------------------
// tabs: a page inside this same origin can open a child window via
// window.open(); track those so `tabs` has something real to list/switch.
// IMPORTANT LIMITATION (see agentBridge.tsx report / SKILL.md): a bridge
// session is scoped to ONE tab's AgentBridgeProvider instance. A child
// window this tracks is a DIFFERENT JS realm with no bridge session of its
// own, so `tabs`/`switch` can only bring it to the OS foreground
// (`.focus()`) — it cannot route subsequent navigate/click/... commands
// into that window. Driving a second tab for real requires the user to
// enable Remote Assist there too and hand over ITS OWN session id.
// ---------------------------------------------------------------------------

const openedTabs = new Map<string, Window>();
let _tabTrackingInstalled = false;

function installTabTracking(): void {
  if (_tabTrackingInstalled) return;
  _tabTrackingInstalled = true;
  const origOpen = window.open.bind(window);
  window.open = ((url?: string | URL, target?: string, features?: string) => {
    const win = origOpen(url, target, features);
    if (win) {
      const key = target && target !== "_blank" && target !== "_self" ? target : `tab-${openedTabs.size + 1}`;
      openedTabs.set(key, win);
    }
    return win;
  }) as typeof window.open;
}
installTabTracking();

// ---------------------------------------------------------------------------
// find: locate elements by ARIA role / accessible name / text, so a caller
// doesn't need a hand-written CSS selector up front. Reuses the same
// "what does this element actually look like" instinct as `read`.
// ---------------------------------------------------------------------------

const IMPLICIT_ROLES: Record<string, string> = {
  a: "link", button: "button", input: "textbox", textarea: "textbox",
  select: "combobox", img: "img", h1: "heading", h2: "heading", h3: "heading",
  h4: "heading", h5: "heading", h6: "heading", li: "listitem", ul: "list",
  ol: "list", nav: "navigation", table: "table", tr: "row", td: "cell",
  th: "columnheader", form: "form", dialog: "dialog", option: "option",
};

function getRole(el: Element): string {
  const explicit = el.getAttribute("role");
  if (explicit) return explicit;
  const tag = el.tagName.toLowerCase();
  if (tag === "input") {
    const type = (el as HTMLInputElement).type;
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "radio";
    if (type === "button" || type === "submit") return "button";
  }
  return IMPLICIT_ROLES[tag] ?? "generic";
}

function getAccessibleName(el: Element): string {
  const ariaLabel = el.getAttribute("aria-label");
  if (ariaLabel) return ariaLabel.trim();
  const labelledBy = el.getAttribute("aria-labelledby");
  if (labelledBy) {
    const parts = labelledBy.split(/\s+/)
      .map((id) => document.getElementById(id)?.textContent?.trim())
      .filter(Boolean);
    if (parts.length) return parts.join(" ");
  }
  if (el.id) {
    const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (label?.textContent?.trim()) return label.textContent.trim();
  }
  const placeholder = el.getAttribute("placeholder");
  if (placeholder) return placeholder.trim();
  const title = el.getAttribute("title");
  if (title) return title.trim();
  return (el.textContent ?? "").trim().slice(0, 200);
}

/** Best-effort unique-ish selector: id > data-testid > a short nth-of-type
 * ancestor chain. Good enough to hand back to a subsequent click/fill/read
 * call, not a claim of global CSS-specificity uniqueness. */
function buildSelector(el: Element): string {
  if (el.id) return `#${CSS.escape(el.id)}`;
  const testId = el.getAttribute("data-testid");
  if (testId) return `[data-testid="${testId}"]`;
  const parts: string[] = [];
  let node: Element | null = el;
  for (let depth = 0; node && depth < 4; depth += 1) {
    if (node.id) {
      parts.unshift(`#${CSS.escape(node.id)}`);
      break;
    }
    const tag = node.tagName.toLowerCase();
    const parentEl: HTMLElement | null = node.parentElement;
    if (!parentEl) {
      parts.unshift(tag);
      break;
    }
    const siblings = Array.from(parentEl.children).filter((c) => c.tagName === node!.tagName);
    const index = siblings.indexOf(node) + 1;
    parts.unshift(siblings.length > 1 ? `${tag}:nth-of-type(${index})` : tag);
    node = parentEl;
    depth += 1;
  }
  return parts.join(" > ");
}

function findElements(
  root: ParentNode, role?: string, name?: string, text?: string,
): Array<{ selector: string; role: string; name: string; tag: string; text: string }> {
  const all = Array.from(root.querySelectorAll("*"));
  const wantRole = role?.toLowerCase().trim();
  const wantName = name?.toLowerCase().trim();
  const wantText = text?.toLowerCase().trim();
  const results: Array<{ selector: string; role: string; name: string; tag: string; text: string }> = [];
  for (const el of all) {
    if (results.length >= 25) break;
    const elRole = getRole(el);
    if (wantRole && elRole !== wantRole) continue;
    const accessibleName = getAccessibleName(el);
    if (wantName && !accessibleName.toLowerCase().includes(wantName)) continue;
    const elText = (el.textContent ?? "").trim();
    if (wantText && !elText.toLowerCase().includes(wantText)) continue;
    if (!wantRole && !wantName && !wantText) continue; // require at least one filter
    results.push({
      selector: buildSelector(el), role: elRole, name: accessibleName,
      tag: el.tagName.toLowerCase(), text: elText.slice(0, 200),
    });
  }
  return results;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function AgentBridgeProvider({ children }: { children: ReactNode }) {
  // Lazy initializer: runs once on mount, so a reload of the SAME tab
  // transparently resumes whatever session was live before the reload
  // instead of starting `null` and looking like the feature turned itself
  // off. The SSE subscribe effect below already depends on `[session, ...]`
  // and re-subscribes whenever `session` changes, so hydrating here is all
  // that's needed — no new SSE plumbing.
  const [session, setSession] = useState<BridgeSession | null>(loadPersistedSession);
  const [enabling, setEnabling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  // executeCommand runs inside an SSE callback, which closes over whatever
  // `session` was at subscribe time — a ref keeps it current without
  // re-subscribing the stream on every render.
  const sessionRef = useRef<BridgeSession | null>(null);
  sessionRef.current = session;

  const enable = useCallback(async () => {
    setEnabling(true);
    setError(null);
    try {
      const s = await api.post<BridgeSession>("/api/agent-bridge/sessions", {
        project: getProject(),
      });
      setSession(s);
      persistSession(s);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setEnabling(false);
    }
  }, []);

  const disable = useCallback(async () => {
    const s = sessionRef.current;
    setSession(null);
    persistSession(null); // explicit end must actually end, not leave a
    // resurrectable stale entry for the next reload to hydrate from.
    if (!s) return;
    try {
      await api.delete(
        `/api/agent-bridge/sessions/${s.id}?token=${encodeURIComponent(s.token)}`,
      );
    } catch {
      // Best-effort — the session's TTL is the backstop if this fails.
    }
  }, []);

  // Tab-close revocation (security posture: "the user can always revoke").
  // beforeunload cannot await a normal fetch, so this uses `keepalive`,
  // which browsers keep alive past unload specifically for this case.
  useEffect(() => {
    const onUnload = () => {
      const s = sessionRef.current;
      if (!s) return;
      persistSession(null); // explicit end must actually end.
      try {
        void fetch(
          `/api/agent-bridge/sessions/${s.id}?token=${encodeURIComponent(s.token)}`,
          { method: "DELETE", keepalive: true },
        );
      } catch {
        /* best-effort */
      }
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, []);

  const executeCommand = useCallback(async (cmd: BridgeCommand) => {
    const s = sessionRef.current;
    if (!s || cmd.session_id !== s.id) return;
    let ok = true;
    let errMsg = "";
    let data: Record<string, unknown> = {};
    try {
      if (cmd.action === "navigate") {
        navigate(cmd.path || "/");
      } else if (cmd.action === "click") {
        const el = resolveSelector(cmd.selector || "");
        if (!el) throw new Error(`no element matches selector: ${cmd.selector}`);
        (el as HTMLElement).click();
      } else if (cmd.action === "fill") {
        const el = resolveSelector(cmd.selector || "");
        if (!el || !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
                     || el instanceof HTMLSelectElement)) {
          throw new Error(`no input/textarea/select matches selector: ${cmd.selector}`);
        }
        setNativeValue(el, cmd.value || "");
      } else if (cmd.action === "read") {
        const el = resolveSelector(cmd.selector || "");
        if (!el) throw new Error(`no element matches selector: ${cmd.selector}`);
        data = {
          text: el.textContent?.trim() ?? "",
          value: (el as HTMLInputElement).value ?? null,
          // Most of this app's state lives in attributes/classes, not text
          // (a pill rail is a strip of empty <button>s colored by class,
          // aria-valuenow carrying the real count) -- textContent alone
          // answers almost no real "what does the screen look like" question
          // an agent asks on the user's behalf. Truncated to stay a small,
          // synchronous SSE payload, not a DOM dump.
          html: (el as HTMLElement).outerHTML?.slice(0, 4000) ?? "",
        };
      } else if (cmd.action === "screenshot") {
        // Renders the REAL live DOM (whatever the user is actually looking
        // at right now) into a canvas from inside this same tab -- no
        // separate/headless browser, no native screen-capture permission
        // prompt. Defaults to the whole page; a selector scopes it to one
        // element (e.g. just the workflow rail) so the payload stays small
        // and the answer stays about the thing that was actually asked.
        const target = cmd.selector
          ? resolveSelector(cmd.selector)
          : document.body;
        if (!target) throw new Error(`no element matches selector: ${cmd.selector}`);
        // html2canvas-pro, not plain html2canvas -- this app's design system
        // (Tailwind v4 + CSS custom properties) resolves colors through
        // oklch()/oklab(), which upstream html2canvas cannot parse at all
        // (throws "unsupported color function" the instant it hits one,
        // and nearly every element here has one somewhere in its computed
        // style chain). html2canvas-pro is the maintained fork that adds
        // oklch/oklab/lab/lch support; same API, drop-in replacement.
        const html2canvas = (await import("html2canvas-pro")).default;
        const canvas = await html2canvas(target as HTMLElement, { scale: 1 });
        data = { image: canvas.toDataURL("image/png") };
      } else if (cmd.action === "hover") {
        // Real pointer + mouse events, in the order a browser actually
        // fires them -- reveals whatever JS-driven hover state the app
        // itself wires up (e.g. Sidebar.tsx's onMouseEnter={loadVersionNotes}).
        // A subsequent `read` on the same/a related selector then sees it.
        // NOTE: a hover surface driven by a pure CSS :hover pseudo-class
        // (no JS listener at all) cannot be revealed this way -- :hover is
        // native pointer-position tracking, not a dispatchable DOM event;
        // that's a genuine gap, not an oversight (see report).
        const el = resolveSelector(cmd.selector || "");
        if (!el) throw new Error(`no element matches selector: ${cmd.selector}`);
        const r = el.getBoundingClientRect();
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        el.dispatchEvent(new PointerEvent("pointerover", { bubbles: true, cancelable: true, clientX: cx, clientY: cy }));
        el.dispatchEvent(new PointerEvent("pointerenter", { bubbles: false, cancelable: true, clientX: cx, clientY: cy }));
        el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true, clientX: cx, clientY: cy }));
        el.dispatchEvent(new MouseEvent("mouseenter", { bubbles: false, cancelable: true, clientX: cx, clientY: cy }));
        el.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, cancelable: true, clientX: cx, clientY: cy }));
        (el as HTMLElement).focus?.();
        data = { hovered: true };
      } else if (cmd.action === "drag") {
        // Real HTML5 drag-and-drop event sequence with a genuine DataTransfer
        // -- proven against WorkflowsPage.tsx's own reorder handlers, which
        // read ev.dataTransfer.effectAllowed on dragstart and call
        // ev.preventDefault() on dragover/drop (both must stay cancelable).
        const source = resolveSelector(cmd.selector || "");
        const target = resolveSelector(cmd.target_selector || "");
        if (!source) throw new Error(`no element matches selector: ${cmd.selector}`);
        if (!target) throw new Error(`no element matches target_selector: ${cmd.target_selector}`);
        const transfer = new DataTransfer();
        const fire = (el: Element, type: string, bubbles = true) =>
          el.dispatchEvent(new DragEvent(type, { bubbles, cancelable: true, dataTransfer: transfer }));
        fire(source, "dragstart");
        fire(target, "dragenter");
        fire(target, "dragover");
        fire(target, "drop");
        fire(source, "dragend");
        data = { dragged: true };
      } else if (cmd.action === "select_option") {
        // Native <select> parity with `fill`, but by option VALUE or its
        // visible label text, so a caller doesn't need to already know the
        // underlying option value attribute.
        const el = resolveSelector(cmd.selector || "");
        if (!el || !(el instanceof HTMLSelectElement)) {
          throw new Error(`no <select> matches selector: ${cmd.selector}`);
        }
        const wanted = cmd.value ?? "";
        const option = Array.from(el.options).find(
          (o) => o.value === wanted || o.textContent?.trim() === wanted,
        );
        if (!option) throw new Error(`no <option> with value/label matching: ${wanted}`);
        setNativeValue(el, option.value);
        data = { value: option.value, label: option.textContent?.trim() ?? "" };
      } else if (cmd.action === "file_upload") {
        const el = resolveSelector(cmd.selector || "");
        if (!el || !(el instanceof HTMLInputElement) || el.type !== "file") {
          throw new Error(`no <input type="file"> matches selector: ${cmd.selector}`);
        }
        const list = cmd.files ?? [];
        if (!list.length) throw new Error("file_upload requires at least one entry in `files`");
        const transfer = new DataTransfer();
        for (const f of list) {
          const bytes = Uint8Array.from(atob(f.content_base64), (c: string) => c.charCodeAt(0));
          transfer.items.add(new File([bytes], f.name, { type: f.type || "application/octet-stream" }));
        }
        el.files = transfer.files;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        data = { files: list.map((f) => f.name) };
      } else if (cmd.action === "press_key") {
        const target: Element = cmd.selector
          ? (resolveSelector(cmd.selector) ?? (() => { throw new Error(`no element matches selector: ${cmd.selector}`); })())
          : (document.activeElement ?? document.body);
        const key = cmd.value || "Enter";
        const opts: KeyboardEventInit = { key, code: key, bubbles: true, cancelable: true };
        target.dispatchEvent(new KeyboardEvent("keydown", opts));
        if (key.length === 1) target.dispatchEvent(new KeyboardEvent("keypress", opts));
        target.dispatchEvent(new KeyboardEvent("keyup", opts));
        data = { key };
      } else if (cmd.action === "handle_dialog") {
        // window.confirm/alert/prompt never actually block this tab -- see
        // installDialogOverride() above. This command PRE-ARMS the decision
        // for the NEXT one (call it before the click/etc. that triggers the
        // dialog) and always reports the most recently resolved dialog, so
        // a driver can confirm it genuinely got accepted/dismissed rather
        // than the page silently freezing or the choice going unrecorded.
        if (cmd.accept !== undefined || cmd.value !== undefined) {
          _dialogPolicy = { accept: cmd.accept ?? true, text: cmd.value };
        }
        const last = dialogLog[dialogLog.length - 1] ?? null;
        data = { armed: _dialogPolicy !== null, last_dialog: last };
      } else if (cmd.action === "wait_for") {
        // Real polling with a real deadline -- never a fixed sleep that
        // either races the DOM or wastes time once the condition is met.
        const timeoutMs = Math.min(Math.max(cmd.timeout_ms ?? 5000, 100), 15000);
        const deadline = Date.now() + timeoutMs;
        let matchedEl: Element | null = null;
        for (;;) {
          matchedEl = resolveSelector(cmd.selector || "");
          if (matchedEl) {
            if (!cmd.text || (matchedEl.textContent ?? "").includes(cmd.text)) break;
            matchedEl = null;
          }
          if (Date.now() >= deadline) {
            throw new Error(
              cmd.text
                ? `timed out waiting for "${cmd.selector}" to contain text: ${cmd.text}`
                : `timed out waiting for selector to appear: ${cmd.selector}`,
            );
          }
          await sleep(100);
        }
        data = { matched: true, text: matchedEl.textContent?.trim() ?? "" };
      } else if (cmd.action === "tabs") {
        // See installTabTracking()'s docstring for the real cross-tab
        // driving limitation -- this can list/foreground a child window
        // this tab opened, never route commands into it.
        if (cmd.value === "switch") {
          const win = cmd.selector ? openedTabs.get(cmd.selector) : undefined;
          if (!win || win.closed) throw new Error(`no open tab tracked as: ${cmd.selector}`);
          win.focus();
          data = { switched: cmd.selector };
        } else {
          data = {
            tabs: Array.from(openedTabs.entries()).map(([tabName, win]) => ({
              name: tabName, closed: win.closed,
            })),
          };
        }
      } else if (cmd.action === "navigate_back") {
        navigate(-1);
        data = { navigated: "back" };
      } else if (cmd.action === "find") {
        const root: ParentNode = cmd.selector ? (resolveSelector(cmd.selector) ?? document) : document;
        data = { matches: findElements(root, cmd.role, cmd.name, cmd.text) };
      } else if (cmd.action === "console") {
        const limit = Math.min(Math.max(cmd.limit ?? 100, 1), RING_LIMIT);
        data = { entries: consoleLog.slice(-limit), total_captured: consoleLog.length };
      } else if (cmd.action === "network") {
        const limit = Math.min(Math.max(cmd.limit ?? 100, 1), RING_LIMIT);
        const entries = networkLog.slice(-limit);
        data = {
          entries,
          total_captured: networkLog.length,
          failed_count: entries.filter((e) => e.status >= 400 || e.status === 0).length,
        };
      } else {
        throw new Error(`unknown action: ${cmd.action}`);
      }
    } catch (e) {
      ok = false;
      errMsg = String((e as Error).message ?? e);
    }
    try {
      await api.post(`/api/agent-bridge/sessions/${s.id}/results`, {
        token: s.token,
        command_id: cmd.command_id,
        ok,
        error: errMsg,
        data,
      });
    } catch {
      // Best-effort — the waiting MCP call simply times out on its own.
    }
  }, [navigate]);

  useEffect(() => {
    if (!session) return;
    const url = `/sse/agent-bridge/${session.id}?token=${encodeURIComponent(session.token)}`;
    const unsubscribe = subscribeStream(url, (raw) => {
      let evt: BridgeCommand;
      try {
        evt = JSON.parse(raw);
      } catch {
        return;
      }
      if (evt.type !== "agent_bridge.command") return;
      void executeCommand(evt);
    });
    return unsubscribe;
  }, [session, executeCommand]);

  return (
    <AgentBridgeContext.Provider value={{ session, enabling, error, enable, disable }}>
      {children}
    </AgentBridgeContext.Provider>
  );
}
