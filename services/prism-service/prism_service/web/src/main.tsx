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
// task 6a094d45: React's lazy machinery can CATCH the import rejection
// before the global listeners above ever see it (the owner's Work click
// no-op'd 2026-08-17 with this recovery present). Vite's own preload
// helper dispatches vite:preloadError on window regardless of who later
// handles the promise — wire it into the same guarded recovery.
window.addEventListener("vite:preloadError", (e) => {
  e.preventDefault();
  recoverFromStaleChunk("Loading chunk failed (vite:preloadError)");
});

const rootElement = document.getElementById("root")!;
createRoot(rootElement).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>,
);

// BOOT WATCHDOG: the shell renders synchronously. If the root is still empty,
// this browser generation is invalid (usually an open tab spanning a watched
// bundle replacement). Reload once; a repeated empty boot renders an explicit
// recovery surface instead of an unexplained white page or reload loop.
window.setTimeout(() => {
  if (rootElement.childElementCount > 0) {
    sessionStorage.removeItem("prism:empty-root-reload");
    return;
  }
  const key = "prism:empty-root-reload";
  const attempted = sessionStorage.getItem(key) === "1";
  if (!attempted) {
    sessionStorage.setItem(key, "1");
    window.location.reload();
    return;
  }
  document.documentElement.className = "dark dark-theme";
  document.documentElement.dataset.theme = "dark";
  rootElement.innerHTML = '<main style="min-height:100vh;background:#0d0f12;color:#f3f4f6;display:grid;place-items:center;font:14px system-ui"><section style="border:1px solid #f59e0b;padding:24px;max-width:460px"><h1 style="margin:0 0 8px;font-size:20px">Application interrupted</h1><p style="margin:0;color:#aeb4bf">The client could not load a consistent bundle. Refresh after the current build completes.</p><button onclick="sessionStorage.removeItem(\'prism:empty-root-reload\');location.reload()" style="margin-top:18px;padding:8px 12px;background:#f59e0b;border:0;color:#111827;cursor:pointer">Retry now</button></section></main>';
}, 4_000);
