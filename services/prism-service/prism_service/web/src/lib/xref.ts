/**
 * Cross-reference (xref) resolver client -- slice S3.
 *
 * Turns a marked code token (a backtick span in markdown, or an OKF evidence
 * file_path) into a routable target by asking the frozen S1 resolver contract:
 *
 *   GET  /api/xref/resolve?token=<s>&project=<p>
 *   POST /api/xref/resolve_batch { project, tokens[] }
 *     -> { kind:"concept"|"symbol"|"file"|"unresolved", label, href, summary?, stale? }
 *
 * Resolution is EAGER (the renderer resolves every chip up front so only the
 * tokens that actually resolve become links -- the rest render as plain code,
 * no broken-link noise) and MICRO-BATCHED: calls made within the same tick are
 * coalesced into ONE /resolve_batch request per project, then deduped per
 * (project, token) for the session. Network/parse failures degrade to an
 * `unresolved` result instead of throwing, so callers always get a typed
 * object.
 */
import { api } from "@/lib/api";
import { getProject } from "@/lib/project";

export type XrefKind = "concept" | "symbol" | "file" | "unresolved";

export interface XrefResolution {
  kind: XrefKind;
  label: string;
  href: string;
  summary?: string;
  stale?: boolean;
}

const _UNRESOLVED = (token: string): XrefResolution => ({
  kind: "unresolved",
  label: token,
  href: "",
});

// Settled/in-flight result per (project, token) -- a doc may carry dozens of
// identical chips, so we never re-hit the endpoint for one we've seen.
const _cache = new Map<string, Promise<XrefResolution>>();

// Tokens queued this tick, with the resolver fns waiting on each.
type Waiter = (r: XrefResolution) => void;
let _queue: { project: string; token: string; resolve: Waiter }[] = [];
let _scheduled = false;

function _flush() {
  _scheduled = false;
  const batch = _queue;
  _queue = [];
  const byProject = new Map<string, { token: string; resolve: Waiter }[]>();
  for (const item of batch) {
    const list = byProject.get(item.project);
    if (list) list.push(item);
    else byProject.set(item.project, [{ token: item.token, resolve: item.resolve }]);
  }
  byProject.forEach((items, project) => {
    const tokens = Array.from(new Set(items.map((i) => i.token)));
    api
      .post<{ results: Record<string, XrefResolution> }>(
        "/api/xref/resolve_batch",
        { project, tokens },
      )
      .then((data) => {
        for (const it of items) {
          it.resolve(data.results?.[it.token] ?? _UNRESOLVED(it.token));
        }
      })
      .catch(() => {
        for (const it of items) it.resolve(_UNRESOLVED(it.token));
      });
  });
}

/**
 * Resolve a single token against the active project (defaults to the SPA's
 * current project via getProject()). Coalesced into a per-tick batch and
 * deduped per (project, token).
 */
export function resolveXref(
  token: string,
  project?: string,
): Promise<XrefResolution> {
  const proj = project ?? getProject();
  const key = `${proj} ${token}`;
  const hit = _cache.get(key);
  if (hit) return hit;
  const p = new Promise<XrefResolution>((resolve) => {
    _queue.push({ project: proj, token, resolve });
    if (!_scheduled) {
      _scheduled = true;
      setTimeout(_flush, 8);
    }
  });
  _cache.set(key, p);
  return p;
}
