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
    | "console" | "network" | "hover" | "drag" | "select_option"
    | "file_upload" | "press_key" | "handle_dialog" | "wait_for"
    | "tabs" | "navigate_back" | "find";
  path?: string;
  selector?: string;
  value?: string;
  target_selector?: string;
  key?: string;
  text?: string;
  role?: string;
  name?: string;
  accept?: boolean;
  files?: Array<{ name: string; type: string; content_base64: string }>;
  tab_action?: "list" | "switch";
  tab_index?: number;
  timeout_ms?: number;
  limit?: number;
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
// Observability: console/error/network capture. Installed unconditionally at
// MODULE LOAD (below, right after the function definitions) -- not gated on
// a bridge session existing or `enable()` ever having run -- so a driver
// that enables Remote Assist, navigates, THEN calls `console` still sees
// what fired during the navigation, not just what fires after.
// ---------------------------------------------------------------------------

type ConsoleEntry = { level: string; message: string; ts: number };
type NetworkEntry = { method: string; url: string; status: number; ok: boolean; ts: number };
type DialogEntry = { kind: string; message: string; ts: number };

const MAX_LOG_ENTRIES = 500;
const consoleLog: ConsoleEntry[] = [];
const networkLog: NetworkEntry[] = [];
const dialogLog: DialogEntry[] = [];
const openedTabs: Window[] = [];

function pushCapped<T>(log: T[], entry: T): void {
  log.push(entry);
  if (log.length > MAX_LOG_ENTRIES) log.shift();
}

function installObservability(): void {
  for (const level of ["log", "warn", "error"] as const) {
    const original = console[level].bind(console);
    console[level] = (...args: unknown[]) => {
      pushCapped(consoleLog, {
        level, message: args.map((a) => String(a)).join(" "), ts: Date.now(),
      });
      original(...args);
    };
  }
  window.addEventListener("error", (e) => {
    pushCapped(consoleLog, { level: "error", message: e.message, ts: Date.now() });
  });
  window.addEventListener("unhandledrejection", (e) => {
    pushCapped(consoleLog, {
      level: "error", message: `unhandled rejection: ${String(e.reason)}`, ts: Date.now(),
    });
  });

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (...args: Parameters<typeof window.fetch>) => {
    const res = await originalFetch(...args);
    pushCapped(networkLog, {
      method: String((args[1] as RequestInit | undefined)?.method || "GET"),
      url: String(args[0]),
      status: res.status,
      ok: res.status < 400,
      ts: Date.now(),
    });
    return res;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (
    this: XMLHttpRequest, method: string, url: string | URL, ...rest: unknown[]
  ) {
    this.addEventListener("loadend", () => {
      pushCapped(networkLog, {
        method, url: String(url), status: this.status, ok: this.status < 400, ts: Date.now(),
      });
    });
    // @ts-expect-error -- variadic forwarding to the native overload set
    return originalOpen.call(this, method, url, ...rest);
  };
}
installObservability();

// ---------------------------------------------------------------------------
// Dialog override: window.confirm/alert/prompt are hijacked unconditionally
// at module load so a native dialog can NEVER actually block the tab (which
// would hang a bridge session with nobody there to click it) -- each call
// resolves immediately from an armed policy, or a safe default if none was
// armed via the `handle_dialog` action.
// ---------------------------------------------------------------------------

let _dialogPolicy: { accept: boolean; text?: string } | null = null;

function installDialogOverride(): void {
  window.confirm = (message?: string) => {
    pushCapped(dialogLog, { kind: "confirm", message: message ?? "", ts: Date.now() });
    const policy = _dialogPolicy;
    _dialogPolicy = null;
    if (!policy) return true; // safe default -- never leave the caller hanging
    return policy.accept;
  };
  window.alert = (message?: string) => {
    pushCapped(dialogLog, { kind: "alert", message: message ?? "", ts: Date.now() });
  };
  window.prompt = (message?: string, defaultValue?: string) => {
    pushCapped(dialogLog, { kind: "prompt", message: message ?? "", ts: Date.now() });
    const policy = _dialogPolicy;
    _dialogPolicy = null;
    if (!policy) return defaultValue ?? null;
    return policy.accept ? policy.text ?? defaultValue ?? "" : null;
  };
}
installDialogOverride();

// ---------------------------------------------------------------------------
// Tab tracking. LIMITATION: window.open() gives this tab a handle to a new
// tab/window it opened, but there is no bridge session or command channel in
// that new tab/window -- we can only list/focus what THIS tab opened, we
// cannot route commands into an arbitrary second tab the way `navigate` etc
// drive this one.
// ---------------------------------------------------------------------------

function installTabTracking(): void {
  const originalOpen = window.open.bind(window);
  window.open = (...args: Parameters<typeof window.open>) => {
    const w = originalOpen(...args);
    if (w) openedTabs.push(w);
    return w;
  };
}
installTabTracking();

// ---------------------------------------------------------------------------
// find: role/name/text search over the live DOM, with a selector generator
// good enough to feed straight back into click/fill/read/hover/etc.
// ---------------------------------------------------------------------------

function getRole(el: Element): string {
  const explicit = el.getAttribute("role");
  if (explicit) return explicit;
  const implicitByTag: Record<string, string> = {
    button: "button", a: "link", input: "textbox", textarea: "textbox",
    select: "combobox", img: "img", h1: "heading", h2: "heading", h3: "heading",
  };
  return implicitByTag[el.tagName.toLowerCase()] || el.tagName.toLowerCase();
}

function getAccessibleName(el: Element): string {
  const ariaLabel = el.getAttribute("aria-label");
  if (ariaLabel) return ariaLabel;
  const labelledBy = el.getAttribute("aria-labelledby");
  if (labelledBy) {
    const labelEl = document.getElementById(labelledBy);
    if (labelEl?.textContent) return labelEl.textContent.trim();
  }
  const id = el.getAttribute("id");
  if (id) {
    const label = document.querySelector(`label[for="${id}"]`);
    if (label?.textContent) return label.textContent.trim();
  }
  const placeholder = el.getAttribute("placeholder");
  if (placeholder) return placeholder;
  const title = el.getAttribute("title");
  if (title) return title;
  return el.textContent?.trim().slice(0, 100) ?? "";
}

function buildSelector(el: Element): string {
  const testId = el.getAttribute("data-testid");
  if (testId) return `[data-testid="${testId}"]`;
  const id = el.getAttribute("id");
  if (id) return `#${id}`;
  const parts: string[] = [];
  let node: Element | null = el;
  while (node && node !== document.body && parts.length < 5) {
    const parent: Element | null = node.parentElement;
    const index = parent ? Array.from(parent.children).indexOf(node) : 0;
    parts.unshift(`${node.tagName.toLowerCase()}:nth-child(${index + 1})`);
    node = parent;
  }
  return parts.join(" > ");
}

type FoundElement = { selector: string; role: string; name: string; text: string };

function findElements(opts: {
  role?: string; name?: string; text?: string; limit: number;
}): FoundElement[] {
  const wantRole = opts.role;
  const wantName = opts.name;
  const wantText = opts.text;
  const results: FoundElement[] = [];
  for (const el of Array.from(document.body.querySelectorAll("*"))) {
    if (!wantRole && !wantName && !wantText) continue;
    const role = getRole(el);
    if (wantRole && role !== wantRole) continue;
    const name = getAccessibleName(el);
    if (wantName && !name.toLowerCase().includes(wantName.toLowerCase())) continue;
    const text = el.textContent?.trim().slice(0, 200) ?? "";
    if (wantText && !text.toLowerCase().includes(wantText.toLowerCase())) continue;
    results.push({ selector: buildSelector(el), role, name, text });
    if (results.length >= opts.limit) break;
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
      } else if (cmd.action === "console") {
        data = { entries: consoleLog.slice(-(cmd.limit ?? 100)) };
      } else if (cmd.action === "network") {
        const entries = networkLog.slice(-(cmd.limit ?? 100));
        const failed_count = entries.filter((e) => e.status >= 400).length;
        data = { entries, failed_count };
      } else if (cmd.action === "hover") {
        const el = resolveSelector(cmd.selector || "");
        if (!el) throw new Error(`no element matches selector: ${cmd.selector}`);
        const rect = (el as HTMLElement).getBoundingClientRect();
        const opts = {
          bubbles: true, cancelable: true,
          clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
        };
        el.dispatchEvent(new PointerEvent("pointerover", opts));
        el.dispatchEvent(new PointerEvent("pointerenter", opts));
        el.dispatchEvent(new MouseEvent("mouseover", opts));
        el.dispatchEvent(new MouseEvent("mouseenter", opts));
        // LIMITATION: a pure-CSS :hover pseudo-class is set by the browser's
        // own hit-testing on real pointer input, not by dispatched events --
        // this only reaches JS-driven hover handlers (onMouseEnter etc).
      } else if (cmd.action === "drag") {
        const source = resolveSelector(cmd.selector || "");
        const target = resolveSelector(cmd.target_selector || "");
        if (!source || !target) {
          throw new Error(`drag needs both selector and target_selector to resolve`);
        }
        const dataTransfer = new DataTransfer();
        const fire = (type: string, el: Element) => el.dispatchEvent(
          new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer }),
        );
        fire("dragstart", source);
        fire("dragenter", target);
        fire("dragover", target);
        fire("drop", target);
        fire("dragend", source);
      } else if (cmd.action === "select_option") {
        const el = resolveSelector(cmd.selector || "");
        if (!el || !(el instanceof HTMLSelectElement)) {
          throw new Error(`no <select> matches selector: ${cmd.selector}`);
        }
        const wanted = cmd.value || "";
        const opt = Array.from(el.options).find(
          (o) => o.value === wanted || o.textContent?.trim() === wanted,
        );
        if (!opt) throw new Error(`no <option> matches value or label: ${wanted}`);
        setNativeValue(el, opt.value);
      } else if (cmd.action === "file_upload") {
        const el = resolveSelector(cmd.selector || "");
        if (!el || !(el instanceof HTMLInputElement) || el.type !== "file") {
          throw new Error(`no <input type="file"> matches selector: ${cmd.selector}`);
        }
        const dataTransfer = new DataTransfer();
        for (const f of cmd.files || []) {
          const binary = atob(f.content_base64);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          dataTransfer.items.add(new File([bytes], f.name, { type: f.type }));
        }
        el.files = dataTransfer.files;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("input", { bubbles: true }));
      } else if (cmd.action === "press_key") {
        const el = (cmd.selector ? resolveSelector(cmd.selector) : null)
          || document.activeElement || document.body;
        const opts = { key: cmd.key || "", bubbles: true, cancelable: true };
        el.dispatchEvent(new KeyboardEvent("keydown", opts));
        el.dispatchEvent(new KeyboardEvent("keyup", opts));
      } else if (cmd.action === "handle_dialog") {
        _dialogPolicy = { accept: cmd.accept ?? true, text: cmd.text };
        data = { last_dialog: dialogLog[dialogLog.length - 1] ?? null };
      } else if (cmd.action === "wait_for") {
        const timeoutMs = cmd.timeout_ms ?? 5000;
        const deadline = Date.now() + timeoutMs;
        let matched = false;
        for (;;) {
          const found = resolveSelector(cmd.selector || "");
          if (found && (!cmd.text || found.textContent?.includes(cmd.text))) {
            matched = true;
            break;
          }
          if (Date.now() >= deadline) {
            throw new Error(`wait_for timed out after ${timeoutMs}ms: ${cmd.selector}`);
          }
          await sleep(150);
        }
        data = { matched };
      } else if (cmd.action === "tabs") {
        if (cmd.tab_action === "switch") {
          const idx = cmd.tab_index ?? 0;
          const w = openedTabs[idx];
          if (!w || w.closed) throw new Error(`no tracked tab at index ${idx}`);
          w.focus();
          data = { switched_to: idx };
        } else {
          data = { tabs: openedTabs.map((w, i) => ({ index: i, closed: w.closed })) };
        }
      } else if (cmd.action === "navigate_back") {
        navigate(-1);
      } else if (cmd.action === "find") {
        const matches = findElements({
          role: cmd.role, name: cmd.name, text: cmd.text, limit: cmd.limit ?? 20,
        });
        data = { matches };
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
