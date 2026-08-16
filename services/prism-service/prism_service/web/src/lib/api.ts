/**
 * Typed fetch wrapper for PRISM's /api surface.
 *
 * Same-origin in prod (FastAPI serves the SPA). Vite dev proxies /api,
 * /sse, /graph to the PRISM service via vite.config.ts.
 */
import { authHeaders } from "@/lib/auth";
// Carries an HTTP status so callers can tell "not signed in" (401) apart from
// a genuine failure. Without it every error was an opaque string and the shell
// could not know to ask for the access key.
export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function fetchJSON<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    // authHeaders() is empty on the machine running PRISM (the server trusts
    // loopback) and carries the bearer key when reached across the network.
    headers: {
      accept: "application/json",
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(
      res.status,
      `${res.status} ${res.statusText}: ${text.slice(0, 200)}`,
    );
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T = unknown>(p: string) => fetchJSON<T>(p),
  post: <T = unknown>(p: string, body: unknown) =>
    fetchJSON<T>(p, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  put: <T = unknown>(p: string, body: unknown) =>
    fetchJSON<T>(p, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  delete: <T = unknown>(p: string) =>
    fetchJSON<T>(p, { method: "DELETE" }),
};

// A plain plan_gate Approve, when the parked evidence is an unapproved
// design packet (isAwaitingDesignApproval, TaskDetailPage.tsx), must also
// record the packet ledger's own approval - or the ledger reads unapproved
// forever even after the gate itself releases (task 791602a9). Same route
// DesignPacket.tsx's own explicit approve button already posts to.
export async function approveDesignPacket(
  taskId: string,
  approver: string,
  project = "default",
): Promise<{ ok: boolean; approver: string }> {
  return api.post(`/api/conductor/design-packet/approve?project=${project}`, {
    task_id: taskId,
    approver,
  });
}

// ── Team-work integration surface (task ae31c2c0) ──────────────────────
// Thin typed helpers over the provider-neutral integration core
// (/api/workspaces/{ws}/integrations/*). The Work view merges native tasks
// with the external entities these return; authorization/sync truth is always
// the server's, never inferred client-side.

export type Workspace = { id: string; name?: string };

// One external work object (GitHub issue/PR, Jira issue) as the API returns it.
// `restricted` is set BY THE SERVER when the caller may see the row exists but
// not its linked context — the UI renders a placeholder, it never guesses.
export type ExternalEntity = {
  id: string;
  workspace_id?: string;
  connection_id?: string;
  entity_kind?: string;       // issue | pull_request | jira_issue
  provider?: string;          // github | jira
  display_key?: string;
  title?: string;
  url?: string;
  remote_status?: string;     // raw provider status (never a local status)
  status_category?: string;
  assignees?: string[];
  task_id?: string;           // linked local intake task, if any
  restricted?: boolean;
};

export type IntegrationConnection = {
  id: string;
  provider?: string;
  remote_scope?: string;
  display_name?: string;
};

export type SyncRun = { id?: string; status?: string; imported?: number };

export async function listWorkspaces(): Promise<Workspace[]> {
  const d = await api.get<{ workspaces: Workspace[] }>("/api/workspaces");
  return d.workspaces ?? [];
}

export async function listIntegrationEntities(
  workspace: string,
  opts: { source?: string; container?: string; task?: string } = {},
): Promise<ExternalEntity[]> {
  const q = new URLSearchParams();
  if (opts.container) q.set("container_id", opts.container);
  if (opts.task) q.set("task_id", opts.task);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  const d = await api.get<{ entities: ExternalEntity[] }>(
    `/api/workspaces/${workspace}/integrations/entities${suffix}`,
  );
  const rows = d.entities ?? [];
  return opts.source ? rows.filter((e) => (e.provider ?? "") === opts.source) : rows;
}

export async function listConnections(
  workspace: string,
): Promise<IntegrationConnection[]> {
  const d = await api.get<{ connections: IntegrationConnection[] }>(
    `/api/workspaces/${workspace}/integrations/connections`,
  );
  return d.connections ?? [];
}

export type ExternalContainer = {
  id: string;
  connection_id?: string;
  kind?: string;
  display_key?: string;
  display_name?: string;
};

export async function listContainers(
  workspace: string,
  connectionId?: string,
): Promise<ExternalContainer[]> {
  const suffix = connectionId ? `?connection_id=${encodeURIComponent(connectionId)}` : "";
  const d = await api.get<{ containers: ExternalContainer[] }>(
    `/api/workspaces/${workspace}/integrations/containers${suffix}`,
  );
  return d.containers ?? [];
}

export async function pullContainer(
  workspace: string,
  containerId: string,
  project: string,
): Promise<SyncRun> {
  return api.post<SyncRun>(
    `/api/workspaces/${workspace}/integrations/containers/${containerId}/pull?project=${encodeURIComponent(project)}`,
    {},
  );
}

// ── Connectors (task e139295d) ─────────────────────────────────────────
// One server-decided status per connector. Claude, GitHub and Jira are PEERS
// (mx-dc7c38) and health is the SERVER's answer, never inferred here from
// whether a connection row happens to exist.

export type ConnectorState =
  | "not_configured" | "not_connected" | "connected" | "needs_attention";

/** The connector card's durable "what did the sync do" (task 1c9899d6) -
 *  the LATEST sync_runs row per tracked container, surfaced server-side
 *  (integration_store.py's own IntegrationStore.list_runs ordering). null
 *  when nothing has synced yet - calmly absent, never fabricated. */
export type LastSync = {
  container: string;
  status: string;
  imported: number;
  reason: string;
};

/** A single tracked collection (repo/project) a connector card links to
 *  (task a752e76c). url is server-supplied, never client-derived. */
export type TrackedCollection = {
  key: string;
  url: string;
};

export type Connector = {
  provider: string;
  name: string;
  state: ConnectorState;
  detail?: string;
  account?: string;
  tracking?: TrackedCollection[];
  /** Whether the user asked PRISM to sync with this provider. Separate from
   *  `state`: a working credential is not consent to sync. */
  sync_enabled?: boolean;
  last_sync?: LastSync | null;
};

/** Turn syncing with a provider on or off. Leaves the connection intact. */
export async function setConnectorSync(
  provider: string, enabled: boolean,
): Promise<boolean> {
  const d = await api.put<{ sync_enabled: boolean }>(
    `/api/integrations/connect/${encodeURIComponent(provider)}/sync`,
    { enabled });
  return d.sync_enabled;
}

/** Track a repository (owner/repo). No team workspace required. */
export async function trackConnectorRepo(
  provider: string, repo: string,
): Promise<{ repo: string }> {
  return api.post(
    `/api/integrations/connect/${encodeURIComponent(provider)}/track`, { repo });
}

/** Run a sync now. Refuses with 409 when syncing is switched off. */
export async function runConnectorSync(
  provider: string, project: string,
): Promise<{ imported: number; runs: { container: string; status: string; imported: number }[] }> {
  return api.post(
    `/api/integrations/connect/${encodeURIComponent(provider)}/sync/run`
    + `?project=${encodeURIComponent(project)}`, {});
}

/** The pre-existing-backlog sweep (task 733af05f). Preview by default —
 *  dry_run creates nothing; pass dry_run:false with explicit task_ids to
 *  confirm live. Two calls by design: never a one-click silent sweep. */
export type BacklogPushReport = {
  provider: string;
  dry_run: boolean;
  scanned: number;
  would_create: string[];
  created: { task_id: string; issue: string; url: string }[];
  skipped: {
    done: string[]; cancelled: string[];
    already_linked: string[]; other_status: string[];
  };
  reason: string;
};

export async function pushBacklog(
  provider: string, project: string,
  opts: { dryRun: boolean; taskIds?: string[] },
): Promise<BacklogPushReport> {
  return api.post<BacklogPushReport>(
    `/api/integrations/connect/${encodeURIComponent(provider)}/push-backlog`
    + `?project=${encodeURIComponent(project)}`,
    { dry_run: opts.dryRun, task_ids: opts.taskIds ?? [] });
}

export async function listConnectorStatus(): Promise<Connector[]> {
  const d = await api.get<{ connectors: Connector[] }>(
    "/api/integrations/connect/status");
  return d.connectors ?? [];
}

/** Is a NEW PRISM task actually reaching GitHub? (task 27e543e0)
 *
 *  Reported link by link, not as one boolean, because the failure this
 *  closes was a feature that LOOKED shipped and was wired nowhere: when a
 *  task does not appear on GitHub, the settings page has to be able to say
 *  which link is missing rather than just "off". */
export type MirrorStatus = {
  enabled: boolean;
  env: string;
  observer_installed: boolean;
  adapters: string[];
  adapter_errors: Record<string, string>;
  scope: string;
  sync_enabled: boolean;
  tracking: string[];
  ready: boolean;
};

export async function getMirrorStatus(): Promise<MirrorStatus> {
  return api.get<MirrorStatus>("/api/integrations/connect/mirror");
}

/** Begin the OAuth round trip; returns the provider authorize URL to open. */
export async function startConnect(provider: string): Promise<string> {
  const d = await api.get<{ authorize_url: string }>(
    `/api/integrations/connect/${provider}/start`);
  return d.authorize_url;
}

/** Connect Jira with a site URL + email + Atlassian API token (task
 *  64ba4755) — no OAuth app needed on this instance. The token never
 *  round-trips back; only the validated account name does. */
export async function connectJiraApiToken(
  siteUrl: string, email: string, apiToken: string,
): Promise<{ connected: boolean; account: string }> {
  return api.post("/api/integrations/connect/jira/api-token",
    { site_url: siteUrl, email, api_token: apiToken });
}
