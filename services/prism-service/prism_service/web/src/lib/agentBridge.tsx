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
 * token this mints is held ONLY in React state (never localStorage — it
 * must not outlive the tab) and is a distinct, narrow credential from the
 * user's general access key.
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
  action: "navigate" | "click" | "fill" | "read" | "screenshot";
  path?: string;
  selector?: string;
  value?: string;
};

type AgentBridgeState = {
  session: BridgeSession | null;
  enabling: boolean;
  error: string | null;
  enable: () => Promise<void>;
  disable: () => Promise<void>;
};

const AgentBridgeContext = createContext<AgentBridgeState | null>(null);

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

/** Set an <input>/<textarea>'s value through React's OWN tracked setter,
 * not the plain DOM property — a plain assignment is invisible to a
 * controlled component's onChange, because React wraps the native setter
 * to notice writes. Dispatching real 'input'/'change' events afterward is
 * what makes this indistinguishable from the person typing. */
function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

export function AgentBridgeProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<BridgeSession | null>(null);
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
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setEnabling(false);
    }
  }, []);

  const disable = useCallback(async () => {
    const s = sessionRef.current;
    setSession(null);
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
        if (!el || !(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) {
          throw new Error(`no input/textarea matches selector: ${cmd.selector}`);
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
        const html2canvas = (await import("html2canvas")).default;
        const canvas = await html2canvas(target as HTMLElement, { scale: 1 });
        data = { image: canvas.toDataURL("image/png") };
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
