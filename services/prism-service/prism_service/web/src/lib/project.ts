/**
 * Active-project hook. Persists in localStorage; broadcasts changes across
 * components via a tiny pub-sub so the header selector and pages stay in sync.
 *
 * Also exposes `notifyProjectsChanged()` so creators/deleters (SettingsPage)
 * can prod the header to re-fetch /api/projects — otherwise the picker stays
 * stale until a page reload.
 *
 * v6.0.20 — `?project=<name>` in the URL is now respected on first read and
 * promoted into localStorage. Before this, every page link / Edge --app
 * launch with `?project=prism` silently fell back to whatever localStorage
 * said (typically `default`), which made the per-project graph + learning
 * pages look broken until the user manually clicked the dropdown.
 */
import { useEffect, useState } from "react";
import { fetchJSON } from "@/lib/api";

const KEY = "prism.project";
const listeners = new Set<(p: string) => void>();
const listChangeListeners = new Set<() => void>();

function _readUrlProject(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const v = new URLSearchParams(window.location.search).get("project");
    return v && v.trim() ? v.trim() : null;
  } catch {
    return null;
  }
}

export function getProject(): string {
  // URL param wins on every read so deep-links keep working; on first
  // resolution, mirror it into localStorage so the rest of the SPA + the
  // header picker reflect the active project without us having to thread
  // the param through every component.
  const url = _readUrlProject();
  if (url) {
    if (localStorage.getItem(KEY) !== url) {
      try { localStorage.setItem(KEY, url); } catch { /* ignore quota */ }
    }
    return url;
  }
  return localStorage.getItem(KEY) || "default";
}

export function setProject(p: string) {
  localStorage.setItem(KEY, p);
  listeners.forEach((fn) => fn(p));
}

let _coldStartResolved = false;

/**
 * One-time cold-start resolver (v6.3.23, follow-up to #137 item 3). Runs
 * ONCE at app mount. When there is NO persisted selection in localStorage
 * AND no `?project=` URL param, fetch /api/projects and persist the
 * non-'default' project with the highest task_count — so /conductor and
 * the other project-scoped views open on a project that actually has work,
 * not the empty 'default' blank state.
 *
 * Explicit choices always win: a persisted localStorage selection or a
 * `?project=` deep-link short-circuit before any fetch (we never override
 * an explicit choice). Falls back to 'default' when every non-default
 * project is empty (all task_counts 0). Best-effort — a fetch failure
 * leaves the existing 'default' fallback untouched.
 */
export async function resolveInitialProject(): Promise<void> {
  if (_coldStartResolved) return;
  _coldStartResolved = true;
  if (typeof window === "undefined") return;
  // Explicit choice wins — never override a deep-link or a prior selection.
  if (_readUrlProject()) return;
  if (localStorage.getItem(KEY)) return;
  try {
    // Through the api chokepoint, never a raw fetch (task c38ef597): every
    // call has to learn to carry a credential, and a bare fetch() here is
    // exactly the one that gets forgotten and then silently 401s.
    const data = await fetchJSON<{
      projects?: string[]; task_counts?: Record<string, number>;
    }>("/api/projects");
    const counts = data.task_counts || {};
    let best: string | null = null;
    let bestCount = 0;
    for (const [name, count] of Object.entries(counts)) {
      if (name === "default") continue;
      if (count > bestCount) { best = name; bestCount = count; }
    }
    // Re-check: another tab / the picker may have persisted while we awaited.
    if (best && bestCount > 0 && !localStorage.getItem(KEY)) {
      setProject(best);
    }
  } catch {
    /* network/parse failure — keep the 'default' fallback */
  }
}

export function useProject(): [string, (p: string) => void] {
  const [p, setP] = useState<string>(getProject);
  useEffect(() => {
    const fn = (v: string) => setP(v);
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  }, []);
  return [p, setProject];
}

/**
 * Fire to tell every subscriber (e.g. the PageHeader picker) that the
 * /api/projects list may have changed — they should re-fetch.
 */
export function notifyProjectsChanged(): void {
  listChangeListeners.forEach((fn) => {
    try { fn(); } catch { /* swallow — UI subscriber crash shouldn't fan out */ }
  });
}

/** Subscribe to projects-list-changed events. Unsubscribes on unmount. */
export function useProjectsListChange(onChange: () => void): void {
  useEffect(() => {
    listChangeListeners.add(onChange);
    return () => { listChangeListeners.delete(onChange); };
  }, [onChange]);
}
