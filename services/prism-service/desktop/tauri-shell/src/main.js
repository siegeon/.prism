// PRISM splash poller — runs in the bundled webview before the backend
// is ready. Polls 127.0.0.1:7778/api/version every 500ms; navigates to
// the main UI on first 200, falls into an error state after a timeout.
//
// Issue #84: previously the window opened directly on http://localhost:7778/
// and rendered a raw ERR_CONNECTION_REFUSED when the backend wasn't up.
// Now the user sees a "Starting backend…" splash, and a clear error with
// install instructions if it never comes up.

const BACKEND_URL = "http://127.0.0.1:7778/";
const HEALTH_URL = "http://127.0.0.1:7778/api/version";
const POLL_MS = 500;
const TIMEOUT_MS = 30_000;

const $ = (id) => document.getElementById(id);

let startedAt = 0;
let cancelled = false;

async function probe() {
  try {
    const r = await fetch(HEALTH_URL, { cache: "no-store" });
    return r.ok;
  } catch {
    return false;
  }
}

function showError(detailText) {
  $("state-starting").hidden = true;
  const err = $("state-error");
  err.hidden = false;
  if (detailText) $("error-detail").textContent = detailText;
}

function showStarting() {
  $("state-error").hidden = true;
  $("state-starting").hidden = false;
}

async function loop() {
  startedAt = Date.now();
  cancelled = false;
  showStarting();
  while (!cancelled) {
    if (await probe()) {
      // Navigate the existing window to the real app — using the
      // bundled splash as the entry point preserves the window
      // chrome and history identity.
      window.location.replace(BACKEND_URL);
      return;
    }
    const elapsed = Date.now() - startedAt;
    $("detail").textContent =
      `Waiting for ${HEALTH_URL} (${Math.floor(elapsed / 1000)}s)`;
    if (elapsed > TIMEOUT_MS) {
      showError(
        `The PRISM service didn't respond on 127.0.0.1:7778 after ` +
        `${Math.floor(TIMEOUT_MS / 1000)} seconds.`
      );
      return;
    }
    await new Promise((res) => setTimeout(res, POLL_MS));
  }
}

window.addEventListener("DOMContentLoaded", () => {
  $("retry").addEventListener("click", () => loop());
  loop();
});
