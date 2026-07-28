/**
 * Typed fetch wrapper for PRISM's /api surface.
 *
 * Same-origin in prod (FastAPI serves the SPA). Vite dev proxies /api,
 * /sse, /graph to the PRISM service via vite.config.ts.
 */
export async function fetchJSON<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
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
  delete: <T = unknown>(p: string) =>
    fetchJSON<T>(p, { method: "DELETE" }),
};

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

export type Connector = {
  provider: string;
  name: string;
  state: ConnectorState;
  detail?: string;
  account?: string;
  tracking?: string[];
};

export async function listConnectorStatus(): Promise<Connector[]> {
  const d = await api.get<{ connectors: Connector[] }>(
    "/api/integrations/connect/status");
  return d.connectors ?? [];
}

/** Begin the OAuth round trip; returns the provider authorize URL to open. */
export async function startConnect(provider: string): Promise<string> {
  const d = await api.get<{ authorize_url: string }>(
    `/api/integrations/connect/${provider}/start`);
  return d.authorize_url;
}
