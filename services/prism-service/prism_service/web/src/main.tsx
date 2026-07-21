import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import "./lib/theme"; // applies the persisted theme before first paint
import App from "./App";

// STALE-BUNDLE SELF-HEAL (owner 2026-07-21: "its not running again").
// A rebuild rewrites the hashed asset names, so a tab holding the previous
// index.html asks for chunks that no longer exist. Every lazy route then dies
// on "Failed to fetch dynamically imported module" and the app looks DEAD
// while the daemon is perfectly healthy — the owner reasonably reads that as
// "the app stopped". Reload ONCE to pick up the new index.html; the session
// flag stops a reload loop if the failure is anything other than staleness.
const RELOAD_FLAG = "prism:chunk-reload";
function recoverFromStaleChunk(reason: unknown): void {
  const text = String((reason as { message?: string })?.message ?? reason ?? "");
  const isChunkError = /dynamically imported module|Importing a module script failed|Loading chunk|ChunkLoadError/i.test(text);
  if (!isChunkError) return;
  // Time-boxed guard, NOT a one-shot flag: if we reloaded moments ago the
  // failure is not staleness (a reload cannot fix a genuinely missing asset)
  // so stop and let the real error surface. Older than the window means a
  // LATER rebuild — self-heal again. Clearing the flag on 'load' instead
  // would be defeated by the reload's own load event and spin forever.
  const COOLDOWN_MS = 30_000;
  try {
    const last = Number(sessionStorage.getItem(RELOAD_FLAG) || 0);
    if (last && Date.now() - last < COOLDOWN_MS) return;
    sessionStorage.setItem(RELOAD_FLAG, String(Date.now()));
  } catch { /* private mode: fall through to a single reload attempt */ }
  window.location.reload();
}
window.addEventListener("unhandledrejection", (e) => recoverFromStaleChunk(e.reason));
window.addEventListener("error", (e) => recoverFromStaleChunk(e.error ?? e.message));

createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>,
);
