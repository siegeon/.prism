/**
 * ReconnectBanner — the persistent "we're updating, hang on" strip that
 * replaces a blank white tab during a backend restart (owner live,
 * 2026-08-24: "it should NEVER go white... it should have the banner
 * letting the customer know it's updating").
 *
 * Mounted once in App.tsx, above everything else in the shell, so it's
 * visible regardless of which route is showing underneath. Renders
 * nothing while the server is reachable — this is a strip, not a
 * full-page takeover, because the current page's own content and any
 * cached state stay exactly as they were; only a genuine reload (once
 * lib/reconnect.ts confirms the server answered) replaces them.
 */
import { useEffect, useState } from "react";
import { isReconnecting, subscribeReconnecting } from "@/lib/reconnect";

export default function ReconnectBanner() {
  const [reconnecting, setReconnecting] = useState(isReconnecting());
  useEffect(() => subscribeReconnecting(setReconnecting), []);

  if (!reconnecting) return null;

  return (
    <div
      role="status"
      className="px-4 py-2 text-[13px] font-medium flex items-center gap-2 shrink-0"
      style={{ background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)" }}
    >
      <span className="h-2 w-2 rounded-full animate-pulse shrink-0" style={{ background: "currentColor" }} />
      PRISM is updating — reconnecting automatically. This page will refresh itself once it's back.
    </div>
  );
}
