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
