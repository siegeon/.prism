/**
 * PI session persistence — display-level conversation snapshots.
 *
 * Structure borrowed from @earendil-works/pi-web-ui (MIT, Mario Zechner),
 * reimplemented for React/Hermes.
 *
 * One snapshot per project under the key `prism.pi.session.{project}`,
 * stored in IndexedDB (localStorage fallback when IDB is unavailable or
 * errors, e.g. private-mode quotas). A page reload restores the visible
 * transcript only: the agent's LLM context deliberately restarts fresh —
 * the human keeps the conversation, the model begins a new run. That is
 * enough (and safer: no replaying stale tool receipts into a small model).
 */
import type { PiItem } from "@/lib/piAgent";

export type PiSessionSnapshot = {
  items: PiItem[];
  /** Minimal agent context: which model the exchange ran on. */
  modelId: string;
  savedAt: number;
};

const DB_NAME = "prism-pi";
const DB_STORE = "sessions";

const key = (project: string) => `prism.pi.session.${project}`;

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => { req.result.createObjectStore(DB_STORE); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("indexedDB open failed"));
  });
}

async function idb<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest): Promise<T> {
  const db = await openDb();
  try {
    return await new Promise<T>((resolve, reject) => {
      const req = run(db.transaction(DB_STORE, mode).objectStore(DB_STORE));
      req.onsuccess = () => resolve(req.result as T);
      req.onerror = () => reject(req.error ?? new Error("indexedDB request failed"));
    });
  } finally {
    db.close();
  }
}

export async function saveSession(project: string, snap: PiSessionSnapshot): Promise<void> {
  try {
    if (typeof indexedDB !== "undefined") {
      await idb("readwrite", (s) => s.put(snap, key(project)));
      return;
    }
  } catch { /* fall through to localStorage */ }
  try { localStorage.setItem(key(project), JSON.stringify(snap)); } catch { /* best-effort */ }
}

export async function loadSession(project: string): Promise<PiSessionSnapshot | null> {
  try {
    if (typeof indexedDB !== "undefined") {
      const got = await idb<PiSessionSnapshot | undefined>("readonly", (s) => s.get(key(project)));
      if (got?.items) return got;
    }
  } catch { /* fall through to localStorage */ }
  try {
    const raw = localStorage.getItem(key(project));
    if (raw) return JSON.parse(raw) as PiSessionSnapshot;
  } catch { /* best-effort */ }
  return null;
}

export async function clearSession(project: string): Promise<void> {
  try {
    if (typeof indexedDB !== "undefined") await idb("readwrite", (s) => s.delete(key(project)));
  } catch { /* best-effort */ }
  try { localStorage.removeItem(key(project)); } catch { /* best-effort */ }
}
