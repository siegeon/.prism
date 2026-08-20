import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ChevronDown, ChevronRight, ExternalLink, FolderTree, GitBranch,
  Github, Loader2, Plus, Search, X,
} from "lucide-react";
import {
  api,
  listConnectorStatus,
  setConnectorSync,
  trackConnectorRepo,
  runConnectorSync,
  startConnect,
  connectJiraApiToken,
  getMirrorStatus,
  pushBacklog,
  listCollaborationSurfaces,
  type Connector,
  type MirrorStatus,
  type BacklogPushReport,
  type CollaborationSurface,
} from "@/lib/api";
import { notifyProjectsChanged, useProject } from "@/lib/project";
import { useJobs, type ScanJob } from "@/lib/scan-activity";
import { Card, Empty, ErrorBanner, Page, SectionLabel } from "@/components/ui";
import { cn } from "@/lib/utils";

/** True when the SPA is loaded inside a Tauri WebView (so the dialog
 * plugin and other Tauri APIs are available). Lets us gracefully degrade
 * the Browse button when someone loads the SPA in a regular browser. */
function isInTauri(): boolean {
  // Tauri 2 injects this on the WebView's window. Safe to access in
  // non-Tauri browsers — returns undefined which falsifies cleanly.
  return typeof (globalThis as any).__TAURI_INTERNALS__ !== "undefined";
}

// "connectors" is its OWN section (mx-dc7c38). Claude is an integration like
// any other, so it appears as a peer card inside Connectors rather than owning
// a section that GitHub and Jira hang beneath.
type SectionId = "projects" | "connectors" | "activity" | "logs" | "service" | "access-key";

const SECTION_META: Record<SectionId, { title: string; description: string }> = {
  connectors: {
    title: "Connectors",
    description: "Places PRISM can connect to. Optional — PRISM tracks your work on its own; connect a provider only if you also want to see work that lives there.",
  },
  "access-key": {
    title: "Access key",
    description: "Your personal key. It is how agents and MCP reach this PRISM, and how you give someone access so they can join you. Readable whenever you are signed in — copy it, or rotate it if it is ever exposed.",
  },
  projects: {
    title: "Projects",
    description: "Add, configure, sync, and delete tracked repos.",
  },
  activity: {
    title: "Background activity",
    description: "Live status of in-process background work: the event-driven memory learning pipeline (throughput, queue depth, in-flight) and the single maintenance clock (last/next sweep), plus the non-memory infra workers (transcript importer, drift reindex, understand drainer, trash sweeper, auto-updater) and the analyzer queue across every project.",
  },
  logs: {
    title: "Logs",
    description: "Every PRISM-initiated claude -p call with timing, tokens, and assistant output.",
  },
  service: {
    title: "Service",
    description: "Container name, version, intervals, MCP endpoint.",
  },
};

const KNOWN_SECTIONS: SectionId[] = ["access-key", "projects", "connectors", "activity", "logs", "service"];

function resolveSection(raw: string | undefined): SectionId {
  // Legacy URL aliases — keep bookmarked links and prior versions working.
  // `auth` (v5.1.8) is now Connections; `jobs` and `workers` are now
  // the unified `activity` page.
  // `connections` was the standalone Claude auth page. Claude now lives on
  // its own card under Connectors, so the old link lands there rather than
  // silently dropping the user on Projects (task c89edbeb).
  if (raw === "auth" || raw === "connections") return "connectors";
  if (raw === "jobs" || raw === "workers") return "activity";
  return (raw && (KNOWN_SECTIONS as string[]).includes(raw)) ? (raw as SectionId) : "projects";
}

type QueueCounts = {
  pending: number;
  in_progress: number;
  completed: number;
  failed: number;
};

type ProjectInfo = {
  name: string;
  mode: "folder" | "clone" | "empty";
  source_path: string | null;
  remote_url: string | null;
  tracked_ref: string | null;
  current_sha: string | null;
  last_analyzed_sha: string | null;
  queue: QueueCounts;
};

const ZERO_QUEUE: QueueCounts = { pending: 0, in_progress: 0, completed: 0, failed: 0 };

async function fetchInfo(name: string): Promise<ProjectInfo> {
  const s = await api.get<{
    mode?: "folder" | "clone" | "empty";
    source_path?: string | null;
    tracked_ref: string;
    remote_url: string | null;
    current_sha: string | null;
    last_analyzed_sha: string | null;
    queue: QueueCounts | null;
  }>(`/api/understand?project=${encodeURIComponent(name)}`);
  return {
    name,
    mode: s.mode ?? (s.source_path ? "folder" : (s.remote_url ? "clone" : "empty")),
    source_path: s.source_path ?? null,
    remote_url: s.remote_url ?? null,
    tracked_ref: s.tracked_ref ?? null,
    current_sha: s.current_sha ?? null,
    last_analyzed_sha: s.last_analyzed_sha ?? null,
    queue: s.queue ?? ZERO_QUEUE,
  };
}

function hasSource(info: ProjectInfo | undefined): boolean {
  return Boolean(info?.source_path || info?.remote_url);
}

function isDrifted(info: ProjectInfo | undefined): boolean {
  if (!info?.current_sha) return false;
  if (!info.last_analyzed_sha) return hasSource(info);
  return info.current_sha !== info.last_analyzed_sha;
}

function queueIsBusy(q: QueueCounts | undefined): boolean {
  if (!q) return false;
  return q.pending > 0 || q.in_progress > 0;
}


export default function SettingsPage() {
  const [active, setActive] = useProject();
  const [projects, setProjects] = useState<string[]>([]);
  const [infos, setInfos] = useState<Record<string, ProjectInfo>>({});
  const [error, setError] = useState<string | null>(null);

  const loadProjects = useCallback(async () => {
    try {
      const r = await api.get<{ projects: string[] }>("/api/projects");
      setProjects(r.projects);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, []);

  const loadAllInfos = useCallback(async (names: string[]) => {
    const rows = await Promise.all(
      names.map((n) => fetchInfo(n).catch(() => null)),
    );
    setInfos((prev) => {
      const next = { ...prev };
      rows.forEach((r, i) => { if (r) next[names[i]] = r; });
      return next;
    });
  }, []);

  const reloadOne = useCallback(async (name: string) => {
    try {
      const info = await fetchInfo(name);
      setInfos((prev) => ({ ...prev, [name]: info }));
    } catch {
      // ignore — card stays with last known data
    }
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);
  useEffect(() => {
    if (projects.length > 0) loadAllInfos(projects);
  }, [projects, loadAllInfos]);


  const { section: sectionParam } = useParams<{ section?: string }>();
  const section: SectionId = resolveSection(sectionParam);
  const meta = SECTION_META[section];

  return (
    <Page>
      <div>
        <h1 className="text-[20px] font-[650] leading-tight tracking-[-0.01em]">{meta.title}</h1>
        <p className="text-sm opacity-60 mt-1">{meta.description}</p>
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      {section === "access-key" && (
        <Card>
          <SectionLabel>Your access key</SectionLabel>
          <AccessKeyPanel />
        </Card>
      )}

      {section === "projects" && (
        <Card>
          <SectionLabel>Projects</SectionLabel>
          <NewProjectRow onCreated={async (name) => {
            await loadProjects();
            await reloadOne(name);
            setActive(name);
            notifyProjectsChanged();
          }} />
          {projects.length === 0 ? (
            <Empty>No projects yet — create one above.</Empty>
          ) : (
            <ul className="divide-y divide-[color:var(--midground-base)]/10">
              {projects.map((p) => (
                <ProjectCard
                  key={p}
                  info={infos[p]}
                  name={p}
                  isActive={p === active}
                  onActivate={() => setActive(p)}
                  onSaved={() => reloadOne(p)}
                  onDeleted={async () => {
                    setInfos((prev) => {
                      const next = { ...prev };
                      delete next[p];
                      return next;
                    });
                    if (p === active) setActive("default");
                    await loadProjects();
                    notifyProjectsChanged();
                  }}
                />
              ))}
            </ul>
          )}
        </Card>
      )}

      {/* CONNECTORS — its own section. Claude, GitHub and Jira are PEERS here
          (mx-dc7c38); integrations are no longer nested under Claude auth. */}
      {section === "connectors" && <ConnectorsSection project={active} />}

      {section === "activity" && (
        <div className="space-y-6">
          <Card>
            <SectionLabel>Health · deadlock watchdog</SectionLabel>
            <WatchdogPanel />
          </Card>
          <Card>
            <SectionLabel>Workers</SectionLabel>
            <BackgroundWorkersPanel />
          </Card>
          <Card>
            <SectionLabel>Jobs</SectionLabel>
            <JobsPanel />
          </Card>
        </div>
      )}

      {section === "logs" && (
        <Card>
          <SectionLabel>Logs</SectionLabel>
          <ClaudeRunsPanel />
        </Card>
      )}

      {section === "service" && (
        <div className="space-y-6">
          <Card>
            <SectionLabel>Updates</SectionLabel>
            <UpdatesPanel />
          </Card>
          <Card>
            <SectionLabel>Service</SectionLabel>
            <ServiceInfoPanel />
          </Card>
        </div>
      )}
    </Page>
  );
}


type UpdateStatus = {
  running_version: string;
  latest_version: string | null;
  latest_published_at: string | null;
  update_available: boolean;
  in_docker: boolean;
  auto_apply_enabled: boolean;
  last_check_at: number;
  last_check_ok: boolean;
  last_error: string;
  restart_required: boolean;
  asset_url: string | null;
  poll_interval_s: number;
};

function UpdatesPanel() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [appliedNotice, setAppliedNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await api.get<UpdateStatus>("/api/update/status");
      setStatus(s);
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  // Poll every 30s so the panel reflects background checks in close to
  // real-time. Cheap — single GET against a cached in-memory dict.
  useEffect(() => {
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const forceCheck = async () => {
    setChecking(true);
    setError(null);
    try {
      const s = await api.post<UpdateStatus>("/api/update/check", {});
      setStatus(s);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setChecking(false);
    }
  };

  const applyNow = async () => {
    setApplying(true);
    setError(null);
    setAppliedNotice(null);
    try {
      const result = await api.post<{
        ok: boolean;
        target_version?: string;
        restart_required?: boolean;
        restart_auto?: boolean;
      }>("/api/update/apply", {});
      if (result.ok) {
        const v = result.target_version ?? "the new version";
        if (result.restart_auto) {
          setAppliedNotice(
            `Installed ${v}. Re-executing the daemon now — page may reconnect.`,
          );
        } else {
          setAppliedNotice(
            `Installed ${v}. Restart required — run \`prism stop && prism start --daemon\` on the host.`,
          );
        }
      }
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setApplying(false);
    }
  };

  if (!status) {
    return error
      ? <div className="text-sm text-[color:var(--accent-rose-fg)]">{error}</div>
      : <div className="text-sm text-[color:var(--text-secondary)]">Loading…</div>;
  }

  const lastCheckLabel = status.last_check_at
    ? new Date(status.last_check_at * 1000).toLocaleString()
    : "never";

  return (
    <div className="space-y-4">
      <dl className="text-sm grid grid-cols-[160px_1fr] gap-y-2">
        <dt className="text-[color:var(--text-label)]">Running</dt>
        <dd>
          <span className="font-serif text-base">v{status.running_version}</span>
        </dd>

        <dt className="text-[color:var(--text-label)]">Latest available</dt>
        <dd>
          {status.latest_version ? (
            <span className="font-mono text-sm">{status.latest_version}</span>
          ) : (
            <span className="text-[color:var(--text-muted)]">unknown</span>
          )}
          {status.latest_published_at && (
            <span className="text-xs text-[color:var(--text-muted)] ml-2">
              published {new Date(status.latest_published_at).toLocaleString()}
            </span>
          )}
        </dd>

        <dt className="text-[color:var(--text-label)]">Status</dt>
        <dd className="text-sm">
          {status.update_available ? (
            <span className="inline-flex items-center gap-2">
              <span className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded-full bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]">
                update available
              </span>
              <span className="text-[color:var(--text-secondary)]">
                {status.auto_apply_enabled
                  ? "auto-apply will trigger on the next sweep"
                  : "manual apply only (PRISM_AUTO_UPDATE=off)"}
              </span>
            </span>
          ) : status.in_docker ? (
            <span className="text-[color:var(--text-secondary)]">
              running in docker — Watchtower handles updates
            </span>
          ) : status.last_check_ok ? (
            <span className="inline-flex items-center gap-2">
              <span className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded-full bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)]">
                up to date
              </span>
              <span className="text-[color:var(--text-secondary)]">
                last checked {lastCheckLabel}
              </span>
            </span>
          ) : (
            <span className="text-[color:var(--accent-rose-fg)]">
              last check failed: {status.last_error || "unknown"}
            </span>
          )}
        </dd>

        <dt className="text-[color:var(--text-label)]">Poll interval</dt>
        <dd className="text-sm">
          every <span className="font-mono">{status.poll_interval_s}s</span>
          {status.poll_interval_s === 0 && (
            <span className="text-[color:var(--text-muted)] ml-2">(disabled)</span>
          )}
        </dd>

        <dt className="text-[color:var(--text-label)]">Auto-apply</dt>
        <dd className="text-sm text-[color:var(--text-secondary)]">
          {status.auto_apply_enabled
            ? "enabled — updates download + install + restart on their own"
            : "disabled (PRISM_AUTO_UPDATE=off) — manual button only"}
        </dd>
      </dl>

      {status.restart_required && (
        <div className="rounded-md border border-[color:var(--accent-amber-ring)] bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] px-3 py-3 text-sm flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="font-serif text-base text-[color:var(--accent-amber-fg)]">
              Update ready — click to restart
            </div>
            <div className="text-xs text-[color:var(--accent-amber-fg)] mt-0.5">
              A new version is installed and waiting. Restart the daemon now to
              flip the served version — if a held-open connection slows the
              drain it is force-closed after a bounded window.
            </div>
          </div>
          <button
            type="button"
            onClick={applyNow}
            disabled={applying}
            className="shrink-0 rounded-md border border-[color:var(--accent-amber-ring)] bg-[color:var(--accent-amber-bg)] hover:bg-[color:var(--accent-amber-bg)] disabled:opacity-50 px-3 py-1.5 text-sm font-medium text-[color:var(--accent-amber-fg)]"
          >
            {applying ? "Restarting…" : "Click to restart"}
          </button>
        </div>
      )}

      {appliedNotice && (
        <div className="rounded-md border border-[color:var(--accent-emerald-ring)] bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] px-3 py-2 text-sm">
          {appliedNotice}
        </div>
      )}

      {error && (
        <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-sm">
          {error}
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={forceCheck}
          disabled={checking || applying}
          className="px-3 py-2 rounded-md border border-[color:var(--border-default)] text-xs uppercase tracking-wider disabled:opacity-40 hover:bg-[color:var(--surface-2)]"
        >
          {checking ? "Checking…" : "Check now"}
        </button>
        {status.update_available && !status.in_docker && (
          <button
            type="button"
            onClick={applyNow}
            disabled={applying || checking}
            className="px-4 py-2 rounded-md bg-[color:var(--text-primary)] text-[color:var(--surface-0)] text-xs uppercase tracking-wider disabled:opacity-40"
          >
            {applying ? "Updating…" : `Update to ${status.latest_version}`}
          </button>
        )}
        {status.in_docker && (
          <span className="text-xs text-[color:var(--text-muted)]">
            apply disabled — docker installs use Watchtower
          </span>
        )}
      </div>
    </div>
  );
}


type ClaudeAuthStatus = {
  authenticated: boolean;
  config_dir: string;
  credentials_path: string;
  container: string;
  login_command: string;
  instructions: string;
};

type ClaudeConfig = {
  project: string;
  claude_project_dir: string;
  source: "auto" | "explicit";
};

/** Per-project Claude transcript source. PRISM normally guesses the
 * ~/.claude/projects/<slug> folder from the host home + a slug of the
 * project path; when that misses (cross-host, odd drive, slug skew) the
 * customer's Claude can report its real dir via the register_claude_source
 * MCP tool, or the dir can be set here. 'explicit' = reported/edited,
 * 'auto' = falling back to slug discovery. Rendered with Hermes primitives,
 * never a raw JSON dump. */
function ClaudeSourceCard({ project }: { project: string }) {
  const [config, setConfig] = useState<ClaudeConfig | null>(null);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const c = await api.get<ClaudeConfig>(
        `/api/memory/claude-config?project=${encodeURIComponent(project)}`,
      );
      setConfig(c);
      setDraft(c.claude_project_dir ?? "");
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, [project]);

  useEffect(() => { load(); }, [load]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await api.post(
        `/api/memory/claude-config?project=${encodeURIComponent(project)}`,
        { claude_project_dir: draft.trim() },
      );
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSaving(false);
    }
  };

  if (!config) {
    return <div className="text-sm opacity-60">Loading…</div>;
  }

  const isExplicit = config.source === "explicit";
  return (
    <form onSubmit={save} className="space-y-3 text-sm">
      <div className="flex items-center gap-2">
        <span className="opacity-60">Project</span>
        <span className="font-mono text-xs">{config.project}</span>
        <span
          className={
            "ml-auto rounded-full px-2 py-0.5 text-2xs uppercase tracking-[0.14em] " +
            (isExplicit
              ? "bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)]"
              : "bg-[color:var(--midground-base)]/10 opacity-70")
          }
        >
          {isExplicit ? "explicit (reported by Claude)" : "auto (slug)"}
        </span>
      </div>
      <p className="text-xs opacity-60 leading-relaxed">
        The ~/.claude/projects/&lt;slug&gt; folder PRISM imports session
        transcripts from. Leave blank to auto-discover from the project path;
        set it (or let Claude report it via register_claude_source) when the
        slug does not match.
      </p>
      <label className="block">
        <span className="text-2xs uppercase tracking-[0.18em] opacity-60">
          claude_project_dir
        </span>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="auto — leave blank to guess from the project path"
          className="mt-1 w-full rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--midground-base)]/[0.03] px-3 py-2 font-mono text-xs"
        />
      </label>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <button
        type="submit"
        disabled={saving}
        className="rounded-md border border-[color:var(--midground-base)]/15 px-3 py-1.5 text-xs hover:bg-[color:var(--midground-base)]/[0.06] disabled:opacity-50"
      >
        {saving ? "Saving…" : "Save source"}
      </button>
    </form>
  );
}

type MyKey = { id: string; key: string; label: string; created_at: string; user_id: string };

function AccessKeyPanel() {
  const [key, setKey] = useState<MyKey | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [rotating, setRotating] = useState(false);

  const load = useCallback(() => {
    api.get<MyKey>("/api/auth/my-key")
      .then((k) => { setKey(k); setErr(null); })
      .catch((e) => setErr(String(e)));
  }, []);
  useEffect(() => { load(); }, [load]);

  const copy = async (text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); }
    catch { /* clipboard blocked — the key is still visible to copy by hand */ }
  };
  const rotate = async () => {
    setRotating(true);
    try { const k = await api.post<MyKey>("/api/auth/my-key/rotate", {}); setKey(k); }
    catch (e) { setErr(String(e)); }
    finally { setRotating(false); }
  };

  if (err) return <ErrorBanner>{err}</ErrorBanner>;
  if (!key) return <Empty>Loading your key…</Empty>;

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const mcpSnippet =
    `claude mcp add --transport http prism "${origin}/mcp/?project=prism" ` +
    `--header "Authorization: Bearer ${key.key}"`;

  return (
    <div className="space-y-4">
      <p className="text-[13px] text-[color:var(--text-secondary)]">
        This is your personal key. Agents and MCP use it to reach this PRISM, and you give
        it to someone so they can join you. It stays readable here whenever you are signed
        in — copy it, or rotate it if it is ever exposed.
      </p>

      <div>
        <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mb-1">Access key</div>
        <div className="flex items-center gap-2">
          <code className="flex-1 min-w-0 truncate rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/40 px-3 py-2 font-mono text-[12.5px] text-[color:var(--accent-teal-fg)]">
            {key.key}
          </code>
          <button
            onClick={() => copy(key.key)}
            className="shrink-0 rounded-md border border-[color:var(--border-default)] px-3 py-2 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>

      <div>
        <div className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] mb-1">Connect a coding agent over MCP</div>
        <div className="flex items-start gap-2">
          <code className="flex-1 min-w-0 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/40 px-3 py-2 font-mono text-[12.5px] leading-relaxed whitespace-pre-wrap break-all text-[color:var(--text-primary)]">
            {mcpSnippet}
          </code>
          <button
            onClick={() => copy(mcpSnippet)}
            className="shrink-0 rounded-md border border-[color:var(--border-default)] px-3 py-2 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10"
          >
            Copy
          </button>
        </div>
      </div>

      <div className="flex items-center gap-3 pt-1">
        <button
          onClick={rotate}
          disabled={rotating}
          className="rounded-md border border-[color:var(--border-default)] px-3 py-2 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10 disabled:opacity-50"
        >
          {rotating ? "Rotating…" : "Rotate key"}
        </button>
        <span className="text-2xs text-[color:var(--text-muted)]">
          Rotating mints a new key and stops the old one. Anything still using the old key must be updated.
        </span>
      </div>
    </div>
  );
}

function ClaudeAuthCard() {
  const [status, setStatus] = useState<ClaudeAuthStatus | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(() => {
    api.get<ClaudeAuthStatus>("/api/claude-auth/status")
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  useEffect(() => { load(); }, [load]);
  // Poll while not authenticated so the card flips to "authenticated"
  // as soon as the operator's docker-exec login completes.
  useEffect(() => {
    if (status?.authenticated) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [status?.authenticated, load]);

  if (!status) {
    return <div className="text-sm opacity-60">Loading…</div>;
  }

  if (status.authenticated) {
    return (
      <div className="space-y-2">
        <div className="inline-flex items-center gap-2 text-sm">
          <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] text-2xs uppercase tracking-wider">
            authenticated
          </span>
          <span className="opacity-70">
            Claude CLI is logged in. The drainer can run analyzers.
          </span>
        </div>
        <div className="text-2xs opacity-50 font-mono">
          credentials → {status.credentials_path}
        </div>
      </div>
    );
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(status.login_command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // ignore clipboard failures
    }
  };

  return (
    <div className="space-y-3">
      <div className="inline-flex items-center gap-2 text-sm">
        <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] text-2xs uppercase tracking-wider">
          not authenticated
        </span>
        <span className="opacity-70">
          Run this once on the host to complete OAuth:
        </span>
      </div>
      <div className="flex items-center gap-2">
        <code className="flex-1 px-3 py-2 rounded-md bg-[color:var(--midground-base)]/[0.04] border border-[color:var(--midground-base)]/15 text-xs font-mono select-all break-all">
          {status.login_command}
        </code>
        <button
          type="button"
          onClick={copy}
          className="px-3 py-2 rounded-md border border-[color:var(--midground-base)]/30 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="text-2xs opacity-60 leading-snug">
        {status.instructions} This panel polls every 5 seconds and will flip
        to <span className="font-mono">authenticated</span> automatically as
        soon as the OAuth flow completes.
      </p>
    </div>
  );
}

type ClaudeUsageWindow = { utilization: number; resets_at: string };
type ClaudeUsageStatus = {
  available: boolean;
  reason: "" | "not_authenticated" | "upstream_unavailable" | "no_usage_data";
  windows: Record<string, ClaudeUsageWindow>;
};

const CLAUDE_USAGE_LABELS: Record<string, string> = {
  five_hour: "Current session",
  seven_day: "Current week · all models",
  seven_day_opus: "Current week · Opus",
  seven_day_sonnet: "Current week · Sonnet",
  seven_day_fable: "Current week · Fable",
};

function ClaudeUsageCard() {
  const [usage, setUsage] = useState<ClaudeUsageStatus | null>(null);

  const load = useCallback(() => {
    api.get<ClaudeUsageStatus>("/api/claude-auth/usage")
      .then(setUsage)
      .catch(() => setUsage({ available: false, reason: "upstream_unavailable", windows: {} }));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 60_000);
    return () => clearInterval(timer);
  }, [load]);

  if (!usage) return <div className="text-sm opacity-60">Loading live usage…</div>;
  if (!usage.available) {
    const message = usage.reason === "not_authenticated"
      ? "Sign in to Claude above to see subscription usage."
      : "Live Claude usage is temporarily unavailable.";
    return <div className="text-sm opacity-60">{message}</div>;
  }

  const rank = (key: string) => key === "five_hour" ? 0 : key === "seven_day" ? 1 : 2;
  const windows = Object.entries(usage.windows)
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b));

  return (
    <div className="grid gap-3 md:grid-cols-2">
      {windows.map(([key, window]) => {
        const pct = Math.max(0, Math.min(100, window.utilization));
        const model = key.replace(/^seven_day_/, "").replaceAll("_", " ");
        const label = CLAUDE_USAGE_LABELS[key]
          ?? (key.startsWith("seven_day_") ? `Current week · ${model}` : key.replaceAll("_", " "));
        const reset = window.resets_at
          ? new Date(window.resets_at).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })
          : "Reset time unavailable";
        return (
          <div key={key} className="rounded-lg border border-[color:var(--border-subtle)] bg-[color:var(--surface-2)]/40 p-3">
            <div className="flex items-baseline justify-between gap-3 mb-2">
              <span className="text-xs font-semibold" style={{ color: "var(--text-primary)" }}>{label}</span>
              <span className="font-mono text-sm tabular-nums" style={{ color: pct >= 90 ? "var(--accent-amber-fg)" : "var(--accent-teal-fg)" }}>
                {Math.round(pct)}%
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-[color:var(--surface-3)]" role="progressbar"
              aria-label={`${label} usage`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
              <div className="h-full rounded-full transition-[width] duration-500"
                style={{ width: `${pct}%`, background: pct >= 90 ? "var(--accent-amber-fg)" : "var(--accent-teal-fg)" }} />
            </div>
            <div className="mt-2 text-2xs" style={{ color: "var(--text-muted)" }}>Resets {reset}</div>
          </div>
        );
      })}
      <p className="md:col-span-2 text-2xs" style={{ color: "var(--text-muted)" }}>
        Live subscription limits from Claude. Refreshes every minute while this panel is open.
      </p>
    </div>
  );
}


type GithubAuthStatus = {
  authenticated: boolean;
  credentials_path: string;
  fingerprint: string;
  instructions: string;
  // OAuth metadata — empty when the active token came from the PAT path.
  login: string;
  avatar_url: string;
  scopes: string;
  connected_at: number;
  // Device-flow setup state — drives the first-time-setup vs Connect button choice.
  client_id_configured: boolean;
  client_id_preview: string;
  register_url: string;
};

type DeviceCode = {
  flow_id: string;
  user_code: string;
  verification_uri: string;
  expires_in: number;
  interval: number;
};

type DevicePoll =
  | { status: "pending"; interval?: number }
  | { status: "success"; login: string }
  | { status: "expired" }
  | { status: "denied" }
  | { status: "error"; error: string };

type GhRepo = {
  full_name: string;
  name: string;
  owner: string;
  private: boolean;
  default_branch: string;
  description: string;
  clone_url: string;
  html_url: string;
  pushed_at: string;
  stargazers_count: number;
};

function GithubAuthCard() {
  const [status, setStatus] = useState<GithubAuthStatus | null>(null);
  const [device, setDevice] = useState<DeviceCode | null>(null);
  const [showPat, setShowPat] = useState(false);
  const [showClientIdSetup, setShowClientIdSetup] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.get<GithubAuthStatus>("/api/github-auth/status")
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  useEffect(() => { load(); }, [load]);

  const startDeviceFlow = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const code = await api.post<DeviceCode>("/api/github-auth/device/start", {});
      setDevice(code);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSubmitting(false);
    }
  };

  const disconnect = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const next = await api.post<GithubAuthStatus>("/api/github-auth/clear", {});
      setStatus(next);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSubmitting(false);
    }
  };

  if (!status) {
    return <div className="text-sm opacity-60">Loading…</div>;
  }

  if (status.authenticated) {
    return (
      <div className="space-y-3">
        <div className="flex items-center gap-3">
          {status.avatar_url ? (
            <img
              src={status.avatar_url}
              alt={status.login}
              className="w-10 h-10 rounded-full border border-[color:var(--midground-base)]/20"
            />
          ) : (
            <div className="w-10 h-10 rounded-full bg-[color:var(--midground-base)]/10 flex items-center justify-center">
              <Github className="w-5 h-5 opacity-70" />
            </div>
          )}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] text-2xs uppercase tracking-wider">
                connected
              </span>
              <span className="text-sm font-semibold">
                {status.login || status.fingerprint || "GitHub"}
              </span>
            </div>
            <div className="text-2xs opacity-60 mt-0.5">
              {status.scopes
                ? <>scopes: <span className="font-mono">{status.scopes}</span></>
                : status.fingerprint
                  ? <>using <span className="font-mono">{status.fingerprint}</span> (PAT)</>
                  : "PRISM can clone private GitHub repos"}
            </div>
          </div>
        </div>
        {error && (
          <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-xs">
            {error}
          </div>
        )}
        <button
          type="button"
          onClick={disconnect}
          disabled={submitting}
          className="inline-flex items-center gap-2 px-3 py-2 rounded-md border border-[color:var(--midground-base)]/30 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10 disabled:opacity-40"
        >
          {submitting && <Loader2 className="w-3 h-3 animate-spin" />}
          {submitting ? "Disconnecting…" : "Disconnect"}
        </button>
      </div>
    );
  }

  // Not connected — three sub-states drive the visual:
  //   1. no client_id    → first-time setup card with deep link to register
  //   2. client_id ok    → big "Connect with GitHub" button (device flow)
  //   3. PAT fallback    → collapsed by default, expandable
  return (
    <div className="space-y-3">
      <div className="inline-flex items-center gap-2 text-sm">
        <span className="inline-flex items-center px-2 py-0.5 rounded-full bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] text-2xs uppercase tracking-wider">
          not connected
        </span>
        <span className="opacity-70">
          Connect once — PRISM uses the token for every clone after that.
        </span>
      </div>

      {!status.client_id_configured || showClientIdSetup ? (
        <ClientIdSetup
          status={status}
          onSaved={() => { setShowClientIdSetup(false); load(); }}
          onCancel={status.client_id_configured ? () => setShowClientIdSetup(false) : undefined}
        />
      ) : (
        <button
          type="button"
          onClick={startDeviceFlow}
          disabled={submitting}
          className="w-full inline-flex items-center justify-center gap-2 px-4 py-3 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-sm uppercase tracking-wider disabled:opacity-30 hover:opacity-90"
        >
          {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Github className="w-4 h-4" />}
          {submitting ? "Starting…" : "Connect with GitHub"}
        </button>
      )}

      {status.client_id_configured && !showClientIdSetup && (
        <div className="text-2xs opacity-50 flex items-center gap-2">
          <span>OAuth App ID: <span className="font-mono">{status.client_id_preview}</span></span>
          <button
            type="button"
            onClick={() => setShowClientIdSetup(true)}
            className="underline hover:no-underline opacity-70 hover:opacity-100"
          >
            change
          </button>
        </div>
      )}

      {error && (
        <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-xs">
          {error}
        </div>
      )}

      <details
        open={showPat}
        onToggle={(e) => setShowPat((e.target as HTMLDetailsElement).open)}
        className="pt-2 border-t border-[color:var(--midground-base)]/10"
      >
        <summary className="text-2xs uppercase tracking-wider opacity-60 cursor-pointer hover:opacity-100 list-none">
          Or paste a Personal Access Token instead →
        </summary>
        <PatFallback onConnected={(next) => { setStatus(next); }} />
      </details>

      {device && (
        <DeviceFlowModal
          device={device}
          onClose={() => setDevice(null)}
          onSuccess={() => { setDevice(null); load(); }}
        />
      )}
    </div>
  );
}


function ClientIdSetup({
  status, onSaved, onCancel,
}: {
  status: GithubAuthStatus;
  onSaved: () => void;
  onCancel?: () => void;
}) {
  const [clientId, setClientId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!clientId.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.post("/api/github-auth/client-id", { client_id: clientId.trim() });
      onSaved();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={save}
      className="rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--midground-base)]/[0.03] p-4 space-y-3"
    >
      <div className="text-2xs uppercase tracking-[0.18em] opacity-60">
        First-time setup
      </div>
      <ol className="text-xs leading-relaxed opacity-85 list-decimal pl-5 space-y-1.5 marker:opacity-50">
        <li>
          <a
            href={status.register_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 underline hover:no-underline"
          >
            Register a PRISM OAuth App on github.com
            <ExternalLink className="w-3 h-3 opacity-70" />
          </a>{" "}
          (any callback URL is fine).
        </li>
        <li>
          On the app's settings page, flip on <span className="font-mono">Enable Device Flow</span>.
        </li>
        <li>Copy the <span className="font-mono">Client ID</span> from the top of the app page and paste it below.</li>
      </ol>
      <input
        value={clientId}
        onChange={(e) => setClientId(e.target.value)}
        placeholder="Ov23li…"
        autoComplete="off"
        spellCheck={false}
        className="w-full px-3 py-2 rounded-md bg-[color:var(--background-base)]/60 border border-[color:var(--midground-base)]/20 text-sm font-mono"
      />
      {error && (
        <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-xs">
          {error}
        </div>
      )}
      <div className="flex items-center justify-end gap-2">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="text-2xs uppercase tracking-wider opacity-70 hover:opacity-100"
          >
            Cancel
          </button>
        )}
        <button
          type="submit"
          disabled={submitting || !clientId.trim()}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-xs uppercase tracking-wider disabled:opacity-30"
        >
          {submitting && <Loader2 className="w-3 h-3 animate-spin" />}
          {submitting ? "Saving…" : "Save Client ID"}
        </button>
      </div>
    </form>
  );
}


function DeviceFlowModal({
  device, onClose, onSuccess,
}: {
  device: DeviceCode;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [poll, setPoll] = useState<DevicePoll>({ status: "pending" });
  const [copied, setCopied] = useState(false);
  const [interval_, setInterval_] = useState(device.interval);

  // Auto-open the GitHub device-confirmation page in a new tab so the
  // user doesn't have to copy/paste a URL too — the code still has to
  // be entered manually but the page is one click closer.
  useEffect(() => {
    try { window.open(device.verification_uri, "_blank", "noopener"); } catch { /* ignore */ }
  }, [device.verification_uri]);

  useEffect(() => {
    if (poll.status !== "pending") return;
    const t = setTimeout(async () => {
      try {
        const next = await api.post<DevicePoll>("/api/github-auth/device/poll", {
          flow_id: device.flow_id,
        });
        setPoll(next);
        if (next.status === "pending" && "interval" in next && next.interval) {
          setInterval_(next.interval);
        }
        if (next.status === "success") onSuccess();
      } catch (e) {
        setPoll({ status: "error", error: String((e as Error).message ?? e) });
      }
    }, interval_ * 1000);
    return () => clearTimeout(t);
  }, [poll, interval_, device.flow_id, onSuccess]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(device.user_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* ignore */ }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-[460px] max-w-[90vw] rounded-lg border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)] p-6 space-y-4 relative shadow-2xl">
        <button
          type="button"
          onClick={onClose}
          className="absolute top-3 right-3 opacity-60 hover:opacity-100"
          aria-label="Close"
        >
          <X className="w-4 h-4" />
        </button>
        <div className="flex items-center gap-2">
          <Github className="w-5 h-5" />
          <h2 className="font-serif text-lg tracking-tight">Connect to GitHub</h2>
        </div>
        <p className="text-sm opacity-80">
          Enter this code on the GitHub page that just opened:
        </p>
        <div className="flex items-center gap-2 justify-center">
          <code className="px-4 py-3 rounded-md bg-[color:var(--midground-base)]/10 border border-[color:var(--midground-base)]/20 text-xl font-mono tracking-[0.3em] select-all">
            {device.user_code}
          </code>
          <button
            type="button"
            onClick={copy}
            className="px-3 py-3 rounded-md border border-[color:var(--midground-base)]/30 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <a
          href={device.verification_uri}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 text-xs underline hover:no-underline opacity-80 hover:opacity-100"
        >
          Re-open <span className="font-mono">{device.verification_uri}</span>
          <ExternalLink className="w-3 h-3" />
        </a>
        <DeviceFlowFooter poll={poll} onClose={onClose} />
      </div>
    </div>
  );
}

function DeviceFlowFooter({ poll, onClose }: { poll: DevicePoll; onClose: () => void }) {
  if (poll.status === "pending") {
    return (
      <div className="flex items-center gap-2 text-xs opacity-70">
        <Loader2 className="w-3 h-3 animate-spin" />
        Waiting for confirmation… this panel will close automatically.
      </div>
    );
  }
  if (poll.status === "success") {
    return (
      <div className="text-xs text-[color:var(--accent-emerald-fg)]">
        Connected as <span className="font-mono">{poll.login}</span>!
      </div>
    );
  }
  if (poll.status === "expired") {
    return (
      <div className="space-y-2">
        <div className="text-xs text-[color:var(--accent-amber-fg)]">
          Code expired — start a new connect attempt.
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-2xs uppercase tracking-wider underline hover:no-underline"
        >
          Close
        </button>
      </div>
    );
  }
  if (poll.status === "denied") {
    return (
      <div className="text-xs text-[color:var(--accent-amber-fg)]">
        You declined the authorization. Close this dialog and try again if that was a mistake.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-xs">
        {poll.error}
      </div>
    </div>
  );
}


function PatFallback({ onConnected }: { onConnected: (s: GithubAuthStatus) => void }) {
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      const next = await api.post<GithubAuthStatus>("/api/github-auth/configure", {
        token: token.trim(),
        user: "x-access-token",
      });
      onConnected(next);
      setToken("");
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} className="mt-3 space-y-2">
      <div className="flex items-center gap-2">
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="ghp_… or github_pat_…"
          autoComplete="off"
          className="flex-1 px-3 py-2 rounded-md bg-[color:var(--background-base)]/60 border border-[color:var(--midground-base)]/20 text-sm font-mono"
        />
        <button
          type="submit"
          disabled={submitting || !token.trim()}
          className="px-3 py-2 rounded-md border border-[color:var(--midground-base)]/30 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10 disabled:opacity-30"
        >
          {submitting && <Loader2 className="w-3 h-3 animate-spin inline mr-1" />}
          {submitting ? "Connecting…" : "Use token"}
        </button>
      </div>
      {error && (
        <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-xs">
          {error}
        </div>
      )}
      <p className="text-2xs opacity-50 leading-snug">
        Create a fine-grained PAT at{" "}
        <a
          href="https://github.com/settings/personal-access-tokens/new"
          target="_blank"
          rel="noreferrer"
          className="underline hover:no-underline"
        >
          github.com/settings/personal-access-tokens/new
        </a>{" "}
        with <span className="font-mono">Contents: read</span> on the repos PRISM should clone.
      </p>
    </form>
  );
}


function RepoPickerModal({
  onPick, onClose,
}: {
  onPick: (url: string, defaultBranch: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [repos, setRepos] = useState<GhRepo[]>([]);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (resetPage: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const nextPage = resetPage ? 1 : page;
      const r = await api.get<{ repos: GhRepo[]; has_more: boolean }>(
        `/api/github-auth/repos?q=${encodeURIComponent(q)}&page=${nextPage}`,
      );
      setRepos((prev) => resetPage ? r.repos : [...prev, ...r.repos]);
      setHasMore(r.has_more);
      if (resetPage) setPage(1);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setLoading(false);
    }
  }, [q, page]);

  useEffect(() => { load(true); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [q]);

  const loadMore = async () => {
    setPage((p) => p + 1);
    setLoading(true);
    try {
      const r = await api.get<{ repos: GhRepo[]; has_more: boolean }>(
        `/api/github-auth/repos?q=${encodeURIComponent(q)}&page=${page + 1}`,
      );
      setRepos((prev) => [...prev, ...r.repos]);
      setHasMore(r.has_more);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-[560px] max-w-[90vw] max-h-[80vh] rounded-lg border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)] p-5 flex flex-col gap-3 shadow-2xl">
        <div className="flex items-center gap-2">
          <Github className="w-5 h-5" />
          <h2 className="font-serif text-lg tracking-tight flex-1">Pick a GitHub repo</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="opacity-60 hover:opacity-100">
            <X className="w-4 h-4" />
          </button>
        </div>
        <label className="flex items-center gap-2 px-3 py-2 rounded-md bg-[color:var(--midground-base)]/[0.04] border border-[color:var(--midground-base)]/15">
          <Search className="w-4 h-4 opacity-50" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="filter by name…"
            className="flex-1 bg-transparent text-sm focus:outline-none"
            autoFocus
          />
        </label>
        {error && (
          <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-xs">
            {error}
          </div>
        )}
        <ul className="flex-1 overflow-y-auto divide-y divide-[color:var(--midground-base)]/10 -mx-2">
          {repos.map((r) => (
            <li key={r.full_name}>
              <button
                type="button"
                onClick={() => onPick(
                  `https://github.com/${r.full_name}`,
                  r.default_branch || "main",
                )}
                className="w-full text-left px-2 py-2 hover:bg-[color:var(--midground-base)]/[0.04]"
              >
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-mono">{r.full_name}</span>
                  {r.private && (
                    <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--midground-base)]/15 opacity-80">
                      private
                    </span>
                  )}
                  <span className="ml-auto text-2xs opacity-50 font-mono">
                    {r.default_branch}
                  </span>
                </div>
                {r.description && (
                  <div className="text-2xs opacity-60 mt-0.5 truncate">{r.description}</div>
                )}
              </button>
            </li>
          ))}
          {repos.length === 0 && !loading && (
            <li className="px-2 py-6 text-center text-sm opacity-60">
              {q ? `No repos match "${q}".` : "No repos found."}
            </li>
          )}
        </ul>
        <div className="flex items-center gap-2">
          {hasMore && (
            <button
              type="button"
              onClick={loadMore}
              disabled={loading}
              className="px-3 py-1.5 rounded-md border border-[color:var(--midground-base)]/20 text-2xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10 disabled:opacity-40"
            >
              {loading ? <Loader2 className="w-3 h-3 animate-spin" /> : "Load more"}
            </button>
          )}
          <span className="text-2xs opacity-50 ml-auto">
            {repos.length} repo{repos.length === 1 ? "" : "s"}
            {loading && <Loader2 className="w-3 h-3 animate-spin inline ml-2" />}
          </span>
        </div>
      </div>
    </div>
  );
}


type ClaudeRun = {
  run_id: string;
  ts_start: number;
  ts_end: number;
  duration_s: number;
  project: string;
  purpose: string;
  exit_code: number;
  tokens_used: number;
  input_tokens: number;
  output_tokens: number;
  // task 45e04fad — the four fields counted once + real cost + model.
  // Optional: pre-fix manifest rows lack them (flagged in the UI).
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  cost_usd?: number;
  model?: string;
  accounting_version?: string;
  final_text: string;
  stderr_excerpt: string;
  stream_path: string;
  stream_bytes: number;
};

// task 45e04fad — /api/claude-runs/summary shape.
type SpendBucket = {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
  cost_usd: number;
  runs: number;
};
type SpendSummary = {
  group_by: string;
  buckets: Record<string, SpendBucket>;
  prefix_runs: number;
};

const _fmtUsd = (n?: number) =>
  "$" + (Number(n) || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// The shared /api/jobs payload (lib/scan-activity.ts's ScanJob) already
// carries every field this page needs — one row shape serves both the
// shared poller's derived ScanActivity summary and this page's raw list.
type Job = ScanJob;

const JOB_STATES: Array<Job["state"] | "all"> = [
  "all", "pending", "in_progress", "failed", "completed",
];

function JobsPanel() {
  // Used to run its own 5s interval timer; now rides the ONE shared
  // /api/jobs poller (task e8fc073b) that every mounted surface shares.
  // The status filter moves client-side over the shared unfiltered
  // newest-200 (was a server-side &status= query param) — an operator
  // glance panel, not a paginated view, so this is a fine trade.
  const { jobs: allJobs, loaded, lastLoaded, refresh } = useJobs();
  const [filter, setFilter] = useState<Job["state"] | "all">("all");
  const [now, setNow] = useState<number>(Date.now());
  const jobs = filter === "all" ? allJobs : allJobs.filter((j) => j.state === filter);

  // Tick the freshness label every second so it ages live, like Workers.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const ageLabel = lastLoaded
    ? `Updated ${Math.max(0, Math.round((now - lastLoaded) / 1000))}s ago`
    : "";
  const inFlight = jobs.filter(
    (j) => j.state === "pending" || j.state === "in_progress",
  ).length;

  if (!loaded) {
    return <div className="text-sm opacity-60">Loading…</div>;
  }

  return (
    <>
      <div className="flex items-center gap-3 mb-3">
        <span className="text-2xs opacity-60 font-mono">
          {jobs.length} job{jobs.length === 1 ? "" : "s"} · {inFlight} in flight
        </span>
        <span className="text-2xs opacity-40 font-mono ml-auto">{ageLabel}</span>
        <button
          onClick={refresh}
          className="text-2xs uppercase tracking-wider px-3 py-1 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40"
        >
          Refresh now
        </button>
      </div>

      <div className="flex items-center gap-2 flex-wrap mb-3">
        <span className="text-2xs uppercase tracking-wider opacity-50">filter:</span>
        {JOB_STATES.map((s) => {
          const isActive = filter === s;
          return (
            <button
              key={s}
              type="button"
              onClick={() => setFilter(s)}
              className={
                "px-2 py-0.5 rounded-full text-2xs uppercase tracking-wider border " +
                (isActive
                  ? "bg-[color:var(--midground-base)]/15 border-[color:var(--midground-base)]/30"
                  : "border-[color:var(--midground-base)]/15 opacity-60 hover:opacity-100")
              }
            >
              {s}
            </button>
          );
        })}
      </div>

      {jobs.length === 0 ? (
        <Empty>
          {filter === "all"
            ? "No analyzer jobs yet — configure a repo on a project to enqueue some."
            : `No jobs in state "${filter}".`}
        </Empty>
      ) : (
        <ul className="divide-y divide-[color:var(--midground-base)]/10">
          {jobs.map((j) => <JobRow key={j.id} job={j} />)}
        </ul>
      )}
    </>
  );
}

// v6.1.1 — collapsible job row. Sibling of WorkerRow above. Analyzer
// prompts are constructed by the drainer at run time and aren't yet
// persisted on AnalysisJob (v6.2 roadmap), so the disclosure currently
// shows the analyzer's purpose + a note pointing at where the prompt
// lives in the codebase. Failed jobs always expand their stderr.
function JobRow({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  const meta = analyzerMeta(job.analyzer);
  const stateLabel = job.state === "in_progress" ? "running" : job.state;
  const failureOpen = job.state === "failed" && job.error;
  return (
    <li className="py-2 text-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left flex items-start gap-3 hover:bg-[color:var(--midground-base)]/[0.03] rounded-md -mx-2 px-2 py-1 cursor-pointer"
        aria-expanded={open}
      >
        <JobStateDot state={job.state} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{meta.title}</span>
            <span className="text-2xs opacity-50 font-mono">{stateLabel}</span>
            {job.attempts > 1 && (
              <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]">
                retried {job.attempts}×
              </span>
            )}
            <span className="text-2xs opacity-40 font-mono ml-auto">
              {open ? "hide ▴" : "show prompt ▾"}
            </span>
          </div>
          <div className="text-xs opacity-60 mt-0.5 leading-relaxed">
            {meta.purpose && <span>{meta.purpose} </span>}
            <span className="opacity-75 font-mono">
              project {job.project} · sha {job.target_sha.slice(0, 10)} · {jobTimestamp(job)}
            </span>
          </div>
        </div>
      </button>
      {(open || failureOpen) && (
        <div className="mt-2 ml-5 space-y-2">
          {open && (
            <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] p-3 text-[12px] leading-relaxed">
              <div className="text-2xs uppercase tracking-wider opacity-50 mb-2 font-mono">
                analyzer prompt — assembled at run time
              </div>
              <p className="opacity-80">
                {meta.purpose || "This analyzer constructs its prompt from the current source tree at run time."}{" "}
                The exact prompt text is built by the drainer
                (<span className="font-mono">prism_service/inference/analyzer_runner.py</span>)
                using the <span className="font-mono">{job.analyzer}</span> template.
                Persisting the rendered prompt on each AnalysisJob so it
                renders here verbatim is on the v6.2 roadmap.
              </p>
              <div className="mt-2 text-2xs opacity-50 font-mono">
                job id <span className="opacity-80">{job.id}</span>
                {job.result_path && (
                  <> · result <span className="opacity-80">{job.result_path}</span></>
                )}
              </div>
            </div>
          )}
          {failureOpen && (
            <pre className="text-2xs whitespace-pre-wrap font-mono bg-[color:var(--accent-rose-bg)] border border-[color:var(--accent-rose-ring)] rounded-md p-3 text-[color:var(--accent-rose-fg)] max-h-[200px] overflow-y-auto">
              {job.error}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}


// Human-readable label + one-line purpose for each analyzer. Keeps job
// rows visually parallel to worker rows ("Transcript importer" + sentence)
// instead of raw module names + identifier soup.
const ANALYZER_META: Record<string, { title: string; purpose: string }> = {
  onboarding_writer: {
    title: "Onboarding writer",
    purpose: "Drafts a README-grade onboarding document, drawing on the tour, architecture, and domain analyzers.",
  },
  tour_builder: {
    title: "Tour builder",
    purpose: "Designs a 5-15-step guided tour that teaches the project's architecture and key concepts.",
  },
  domain_analyzer: {
    title: "Domain analyzer",
    purpose: "Extracts the project's domain glossary — the nouns and verbs that describe what the system does in its own language.",
  },
  architecture_analyzer: {
    title: "Architecture analyzer",
    purpose: "Classifies every analyzed file into architectural layers (presentation, application, domain, infrastructure).",
  },
};

function analyzerMeta(name: string): { title: string; purpose: string } {
  const known = ANALYZER_META[name];
  if (known) return known;
  // Fallback for unknown analyzers — snake_case → Title case.
  const title = name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  return { title, purpose: "" };
}

function JobStateDot({ state }: { state: Job["state"] }) {
  // Mirror the worker status-dot styling so the rows look like siblings.
  // Color encodes state: amber pending, sky in-progress, emerald completed,
  // rose failed.
  const palette: Record<Job["state"], string> = {
    pending: "bg-[color:var(--accent-amber-fg)] shadow-[0_0_6px_2px_rgba(251,191,36,0.4)]",
    in_progress: "bg-sky-400 shadow-[0_0_6px_2px_rgba(56,189,248,0.4)]",
    completed: "bg-[color:var(--accent-emerald-fg)] shadow-[0_0_6px_2px_rgba(52,211,153,0.4)]",
    failed: "bg-[color:var(--accent-rose-fg)] shadow-[0_0_6px_2px_rgba(251,113,133,0.4)]",
    // ScanJob's state is a superset (task e8fc073b widened Job = ScanJob);
    // cancelled jobs get a neutral dot, same family as a dismissed row.
    cancelled: "bg-[color:var(--midground-base)]/40",
  };
  return (
    <span
      className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${palette[state]}`}
      aria-label={state}
    />
  );
}

function jobTimestamp(j: Job): string {
  // Pick the most recent meaningful timestamp for the row's right gutter.
  const ts =
    j.state === "completed" || j.state === "failed" || j.state === "cancelled"
      ? j.completed_at
      : j.state === "in_progress"
        ? j.started_at
        : j.enqueued_at;
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}


type ServiceInfo = {
  version: string;
  notes: string;
  runtime?: "docker" | "native";
  data_dir?: string;
  container: string;
  claude_config_dir: string;
  claude_authenticated: boolean;
  github_authenticated: boolean;
  github_credentials_path: string;
  understand_drain_interval_s: number;
  drift_interval_s: number;
  governance_interval_s: number;
  quality_interval_s: number;
  mcp_endpoint: string;
};

// Phase 5 (epic 4fd1e6b4): per-pass consolidation receipt — one-line summary
// + click-to-expand detail (progressive disclosure, no raw JSON wall).
type ConsolidationReceipt = {
  extracted: number;
  deduped: number;
  superseded: number;
  retired: number;
  at?: string | null;
};

type Worker = {
  id: string;
  // Phase 5: the memory concern collapses into a "pipeline" row (event
  // throughput) + a "clock" row (maintenance heartbeat). Everything else is
  // a plain "worker" row. The panel branches its render on this.
  kind?: "worker" | "pipeline" | "clock";
  label: string;
  running: boolean;
  cadence_s: number;
  description: string;
  // Pipeline-row readouts (kind === "pipeline").
  events_per_min?: number;
  queue_depth?: number;
  last_event_ts?: number | null;
  in_flight?: number;
  receipt?: ConsolidationReceipt | null;
  // Clock-row readouts (kind === "clock").
  interval_s?: number;
  last_sweep?: number | null;
  next_sweep?: number | null;
  // v6.1.1 — workers that shell out to claude carry a prompt so the
  // drilldown can render the actual instructions driving them. Values:
  //   "static"   — `prompt` is the literal template the worker sends
  //   "dynamic"  — prompt is assembled per-run; `prompt` explains where
  //                to look or what input it draws from
  //   "per_job"  — the worker dispatches arbitrary jobs whose prompts
  //                vary; click an individual job below to see its prompt
  //   "none"     — pure Python work, no LLM involved
  prompt_kind?: "static" | "dynamic" | "per_job" | "none";
  prompt?: string;
};

// Backend-driven panel: renders whatever GET /api/consolidation/workers
// returns. Phase 4 (epic 4fd1e6b4) folded the prior 4-5 separate memory
// timers into ONE consolidated "Memory maintenance clock" (id maintenance_clock),
// so this panel now surfaces that single Maintenance clock row in place of the
// old governance_timer + adaptive_policy_worker entries.
// GH #155 — the deadlock-watchdog health card. One-line summary (armed +
// last probe latency + consecutive failures) with click-to-expand detail
// (last dump time, restart count, the raw status dict) — progressive
// disclosure, Hermes primitives, never a raw stringified dump.
type WatchdogStatus = {
  last_probe_ok: boolean | null;
  last_probe_latency_ms: number | null;
  consecutive_failures: number;
  last_dump_at: number | null;
  dump_count: number;
  restarts: number;
  armed: boolean;
  enabled: boolean;
  interval_s: number;
  timeout_s: number;
  kill_enabled: boolean;
};

function fmtAgo(ts: number | null, now: number): string {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(now / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function WatchdogPanel() {
  const [st, setSt] = useState<WatchdogStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState<number>(Date.now());

  const load = useCallback(async () => {
    try {
      setSt(await api.get<WatchdogStatus>("/api/watchdog"));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => {
    load();
    const poll = setInterval(load, 10000);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearInterval(poll); clearInterval(tick); };
  }, [load]);

  if (error && st === null) return <div className="text-sm text-[color:var(--accent-rose-fg)]">{error}</div>;
  if (st === null) return <div className="text-sm opacity-60">Loading…</div>;

  const probeOk = st.last_probe_ok;
  const dotClass = !st.enabled
    ? "bg-[color:var(--accent-slate-fg)]"
    : probeOk === false || st.consecutive_failures > 0
      ? "bg-[color:var(--accent-rose-fg)] shadow-[0_0_6px_2px_rgba(251,113,133,0.4)]"
      : probeOk === true
        ? "bg-[color:var(--accent-emerald-fg)] shadow-[0_0_6px_2px_rgba(52,211,153,0.4)]"
        : "bg-[color:var(--accent-amber-fg)] shadow-[0_0_6px_2px_rgba(251,191,36,0.4)]";
  const latency = st.last_probe_latency_ms != null
    ? `${st.last_probe_latency_ms.toFixed(0)}ms` : "—";
  const summary = !st.enabled
    ? "Watchdog disabled (PRISM_WATCHDOG=off)"
    : probeOk == null
      ? `Armed · probing every ${st.interval_s}s (awaiting first probe)`
      : probeOk
        ? `Healthy · last probe ${latency} · armed=${st.armed}`
        : `WEDGE SUSPECTED · ${st.consecutive_failures} consecutive failure(s) · ${st.dump_count} dump(s)`;

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left flex items-start gap-3 hover:bg-[color:var(--midground-base)]/[0.03] rounded-md -mx-2 px-2 py-1 cursor-pointer"
        aria-expanded={open}
      >
        <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${dotClass}`} aria-hidden />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">Deadlock watchdog</span>
            <span className="text-2xs opacity-50 font-mono">
              probe {st.interval_s}s · timeout {st.timeout_s}s
            </span>
            {st.kill_enabled && (
              <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]">
                self-heal armed
              </span>
            )}
          </div>
          <div className="text-[12px] opacity-70 mt-0.5">{summary}</div>
        </div>
        <span className="text-2xs opacity-40 mt-1">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <dl className="mt-3 ml-5 grid grid-cols-2 gap-x-6 gap-y-1.5 text-[12px]">
          <WdRow k="Last probe" v={probeOk == null ? "—" : probeOk ? "healthy" : "HUNG"} />
          <WdRow k="Last latency" v={latency} />
          <WdRow k="Consecutive failures" v={String(st.consecutive_failures)} />
          <WdRow k="Dump count" v={String(st.dump_count)} />
          <WdRow k="Last dump" v={fmtAgo(st.last_dump_at, now)} />
          <WdRow k="Restarts" v={String(st.restarts)} />
          <WdRow k="Armed" v={st.armed ? "yes" : "no"} />
          <WdRow k="Self-heal (kill)" v={st.kill_enabled ? "on" : "off"} />
        </dl>
      )}
    </div>
  );
}

function WdRow({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt className="text-[color:var(--text-muted)]">{k}</dt>
      <dd className="font-mono text-[color:var(--text-primary)]">{v}</dd>
    </>
  );
}

function BackgroundWorkersPanel() {
  const [workers, setWorkers] = useState<Worker[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [lastLoaded, setLastLoaded] = useState<number | null>(null);
  const [now, setNow] = useState<number>(Date.now());

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const d = await api.get<{ workers: Worker[] }>("/api/consolidation/workers");
      setWorkers(d.workers ?? []);
      setLastLoaded(Date.now());
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
    // Cheap GET against a small in-memory dict; 10s keeps the dots
    // responsive without taxing the daemon.
    const poll = setInterval(load, 10000);
    // Tick the "Xs ago" label every second.
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearInterval(poll); clearInterval(tick); };
  }, [load]);

  const ageLabel = lastLoaded
    ? `Updated ${Math.max(0, Math.round((now - lastLoaded) / 1000))}s ago`
    : "";

  if (error && workers === null) {
    return <div className="text-sm text-[color:var(--accent-rose-fg)]">{error}</div>;
  }
  if (workers === null) {
    return <div className="text-sm opacity-60">Loading…</div>;
  }
  const running = workers.filter((w) => w.running).length;
  return (
    <>
      <div className="flex items-center gap-3 mb-3">
        <span className="text-2xs opacity-60 font-mono">
          {workers.length} worker{workers.length === 1 ? "" : "s"} · {running} running
        </span>
        <span className="text-2xs opacity-40 font-mono ml-auto">{ageLabel}</span>
        <button
          onClick={load}
          disabled={refreshing}
          className="text-2xs uppercase tracking-wider px-3 py-1 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40"
        >
          {refreshing ? "refreshing…" : "Refresh now"}
        </button>
      </div>
      {workers.length === 0 ? (
        <Empty>No workers reported.</Empty>
      ) : (
      <ul className="divide-y divide-[color:var(--midground-base)]/10">
      {workers.map((w) =>
        w.kind === "pipeline" ? (
          <PipelineRow key={w.id} worker={w} now={now} />
        ) : w.kind === "clock" ? (
          <ClockRow key={w.id} worker={w} now={now} />
        ) : (
          <WorkerRow key={w.id} worker={w} />
        )
      )}
    </ul>
      )}
    </>
  );
}


// v6.1.1 — collapsible row that reveals the worker's claude prompt (or
// a kind-specific explanation if no static prompt exists). Mirrors the
// JobRow disclosure below so Workers and Jobs feel like siblings.
function WorkerRow({ worker }: { worker: Worker }) {
  const [open, setOpen] = useState(false);
  const kind = worker.prompt_kind ?? "none";
  const canExpand = kind !== "none";
  return (
    <li className="py-2 text-sm">
      <button
        type="button"
        onClick={() => canExpand && setOpen((v) => !v)}
        disabled={!canExpand}
        className={
          "w-full text-left flex items-start gap-3 " +
          (canExpand ? "hover:bg-[color:var(--midground-base)]/[0.03] rounded-md -mx-2 px-2 py-1 cursor-pointer" : "")
        }
        aria-expanded={open}
      >
        <span
          className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${
            worker.running
              ? "bg-[color:var(--accent-emerald-fg)] shadow-[0_0_6px_2px_rgba(52,211,153,0.4)]"
              : "bg-[color:var(--accent-rose-fg)] shadow-[0_0_6px_2px_rgba(251,113,133,0.4)]"
          }`}
          aria-label={worker.running ? "running" : "not running"}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{worker.label}</span>
            {worker.cadence_s > 0 && (
              <span className="text-2xs opacity-50 font-mono">
                every {worker.cadence_s < 60
                  ? `${worker.cadence_s}s`
                  : `${Math.round(worker.cadence_s / 60)}m`}
              </span>
            )}
            {!worker.running && (
              <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)]">
                not running
              </span>
            )}
            <PromptKindBadge kind={kind} />
            {canExpand && (
              <span className="text-2xs opacity-40 font-mono ml-auto">
                {open ? "hide prompt ▴" : "show prompt ▾"}
              </span>
            )}
          </div>
          <div className="text-xs opacity-60 mt-0.5 leading-relaxed">{worker.description}</div>
        </div>
      </button>
      {canExpand && open && (
        <div className="mt-2 ml-5 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] p-3 text-[12px] leading-relaxed">
          <div className="text-2xs uppercase tracking-wider opacity-50 mb-2 font-mono">
            {kind === "static" && "claude prompt — sent verbatim each cycle (with name/description substituted)"}
            {kind === "dynamic" && "prompt construction — assembled per-run"}
            {kind === "per_job" && "per-job prompts — see individual job rows below"}
          </div>
          <div className="whitespace-pre-wrap font-mono text-[12px] opacity-90">
            {worker.prompt || "(no prompt provided)"}
          </div>
        </div>
      )}
    </li>
  );
}

// Phase 5: shared bits for the collapsed pipeline + clock rows.
function StatusDot({ running }: { running: boolean }) {
  return (
    <span
      className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${
        running
          ? "bg-[color:var(--accent-emerald-fg)] shadow-[0_0_6px_2px_rgba(52,211,153,0.4)]"
          : "bg-[color:var(--accent-rose-fg)] shadow-[0_0_6px_2px_rgba(251,113,133,0.4)]"
      }`}
      aria-label={running ? "running" : "not running"}
    />
  );
}

function Readout({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col">
      <span className="text-[16px] font-mono leading-none">{value}</span>
      <span className="text-2xs uppercase tracking-wider opacity-50 mt-1">{label}</span>
    </div>
  );
}

function agoLabel(ts: number | null | undefined, now: number): string {
  if (!ts) return "never";
  const secs = Math.max(0, Math.round(now / 1000 - ts));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

function inLabel(ts: number | null | undefined, now: number): string {
  if (!ts) return "—";
  const secs = Math.round(ts - now / 1000);
  if (secs <= 0) return "due now";
  if (secs < 60) return `in ${secs}s`;
  if (secs < 3600) return `in ${Math.round(secs / 60)}m`;
  return `in ${Math.round(secs / 3600)}h`;
}

// Event-driven learning pipeline row — events/min, queue depth, last event,
// in-flight count + a progressive-disclosure consolidation receipt.
function PipelineRow({ worker, now }: { worker: Worker; now: number }) {
  const [open, setOpen] = useState(false);
  const r = worker.receipt ?? null;
  const summary = r
    ? `${r.extracted} extracted · ${r.deduped} deduped · ${r.superseded} superseded · ${r.retired} retired`
    : "no consolidation passes yet";
  return (
    <li className="py-3 text-sm">
      <div className="flex items-start gap-3">
        <StatusDot running={worker.running} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{worker.label}</span>
            <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-sky-500/15 text-sky-300/90">
              pipeline
            </span>
            {!worker.running && (
              <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)]">
                not running
              </span>
            )}
          </div>
          <div className="text-xs opacity-60 mt-0.5 leading-relaxed">{worker.description}</div>
          <div className="mt-3 grid grid-cols-4 gap-4">
            <Readout label="events/min" value={Math.round(worker.events_per_min ?? 0)} />
            <Readout label="queue depth" value={worker.queue_depth ?? 0} />
            <Readout label="last event" value={agoLabel(worker.last_event_ts, now)} />
            <Readout label="in-flight" value={worker.in_flight ?? 0} />
          </div>
          {/* Progressive-disclosure consolidation receipt: one-line summary,
              click to expand the per-pass detail. No raw JSON. */}
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="mt-3 w-full text-left flex items-center gap-2 text-[12px] rounded-md -mx-2 px-2 py-1 hover:bg-[color:var(--midground-base)]/[0.03] cursor-pointer"
            aria-expanded={open}
          >
            <span className="text-2xs uppercase tracking-wider opacity-50">last consolidation</span>
            <span className="font-mono opacity-80 truncate">{summary}</span>
            <span className="text-2xs opacity-40 font-mono ml-auto">
              {open ? "hide ▴" : "detail ▾"}
            </span>
          </button>
          {open && r && (
            <div className="mt-2 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] p-3 grid grid-cols-4 gap-4">
              <Readout label="extracted" value={r.extracted} />
              <Readout label="deduped" value={r.deduped} />
              <Readout label="superseded" value={r.superseded} />
              <Readout label="retired" value={r.retired} />
              {r.at && (
                <div className="col-span-4 text-2xs opacity-50 font-mono">
                  ran {agoLabel(Date.parse(r.at) / 1000, now)}
                </div>
              )}
            </div>
          )}
          {open && !r && (
            <div className="mt-2 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] p-3 text-[12px] opacity-60">
              No consolidation pass has run yet — receipts appear here once the
              pipeline mints or supersedes memories.
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

// Maintenance-clock row — interval, last sweep, next sweep.
function ClockRow({ worker, now }: { worker: Worker; now: number }) {
  const interval = worker.interval_s ?? worker.cadence_s ?? 0;
  const intervalLabel = interval < 60 ? `${interval}s` : `${Math.round(interval / 60)}m`;
  return (
    <li className="py-3 text-sm">
      <div className="flex items-start gap-3">
        <StatusDot running={worker.running} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium">{worker.label}</span>
            <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)]">
              clock
            </span>
            {!worker.running && (
              <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)]">
                not running
              </span>
            )}
          </div>
          <div className="text-xs opacity-60 mt-0.5 leading-relaxed">{worker.description}</div>
          <div className="mt-3 grid grid-cols-3 gap-4">
            <Readout label="interval" value={intervalLabel} />
            <Readout label="last sweep" value={agoLabel(worker.last_sweep, now)} />
            <Readout label="next sweep" value={inLabel(worker.next_sweep, now)} />
          </div>
        </div>
      </div>
    </li>
  );
}

function PromptKindBadge({ kind }: { kind: NonNullable<Worker["prompt_kind"]> }) {
  if (kind === "none") return null;
  const styles: Record<string, string> = {
    static: "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)]",
    dynamic: "bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]",
    per_job: "bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)]",
  };
  const labels: Record<string, string> = {
    static: "claude · static prompt",
    dynamic: "claude · dynamic prompt",
    per_job: "claude · per-job prompt",
  };
  return (
    <span className={"text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded " + styles[kind]}>
      {labels[kind]}
    </span>
  );
}


function ServiceInfoPanel() {
  const [info, setInfo] = useState<ServiceInfo | null>(null);

  useEffect(() => {
    api.get<ServiceInfo>("/api/service-info")
      .then(setInfo)
      .catch(() => setInfo(null));
  }, []);

  if (!info) {
    return <div className="text-sm opacity-60">Loading…</div>;
  }
  return (
    <div className="space-y-6">
      {/* Runtime info — operationally useful fields the SPA needs at a glance. */}
      <dl className="text-sm grid grid-cols-[160px_1fr] gap-y-2">
        <dt className="opacity-60">Version</dt>
        <dd className="font-serif text-base tracking-tight">
          PRISM v{info.version}
        </dd>

        <dt className="opacity-60">Container</dt>
        <dd className="font-mono text-xs">{info.container}</dd>

        <dt className="opacity-60">Claude auth</dt>
        <dd className="text-xs">
          {info.claude_authenticated
            ? <span className="text-[color:var(--accent-emerald-fg)]">logged in</span>
            : <span className="text-[color:var(--accent-amber-fg)]">not logged in — see Connections tab</span>}
          <div className="opacity-60 font-mono mt-1">
            config dir → {info.claude_config_dir}
          </div>
        </dd>

        <dt className="opacity-60">GitHub</dt>
        <dd className="text-xs">
          {info.github_authenticated
            ? <span className="text-[color:var(--accent-emerald-fg)]">connected</span>
            : <span className="text-[color:var(--accent-amber-fg)]">not connected — see Connections tab</span>}
          <div className="opacity-60 font-mono mt-1">
            credentials → {info.github_credentials_path}
          </div>
        </dd>

        <dt className="opacity-60">Drainer poll</dt>
        <dd className="text-xs">
          every <span className="font-mono">{info.understand_drain_interval_s}s</span>
          {info.understand_drain_interval_s === 0 && (
            <span className="opacity-60 ml-2">(disabled)</span>
          )}
        </dd>

        <dt className="opacity-60">Background timers</dt>
        <dd className="text-xs space-y-0.5">
          <div>drift reindex: <span className="font-mono">{info.drift_interval_s}s</span></div>
          <div>governance: <span className="font-mono">{info.governance_interval_s}s</span></div>
          <div>quality: <span className="font-mono">{info.quality_interval_s}s</span></div>
        </dd>

        <dt className="opacity-60">MCP</dt>
        <dd className="font-mono text-xs">{info.mcp_endpoint}</dd>
      </dl>

      {/* Release notes — borrows the OnboardingView markdown chrome:
          font-serif headings, opacity-85 body, monospace chips for inline
          code in backticks. Each version becomes its own block. */}
      {info.notes && (
        <div className="space-y-5 max-w-[840px]">
          <div className="text-2xs uppercase tracking-[0.18em] opacity-50">
            Release notes
          </div>
          <ReleaseNotes notes={info.notes} latest={info.version} />
        </div>
      )}
    </div>
  );
}


type Release = { version: string; body: string };

function ReleaseNotes({ notes, latest }: { notes: string; latest: string }) {
  const releases = splitReleases(notes);
  if (releases.length === 0) {
    return <p className="text-sm leading-relaxed opacity-85">{renderInlineNotes(notes)}</p>;
  }
  return (
    <div className="space-y-5">
      {releases.map((r, idx) => (
        <ReleaseBlock
          key={r.version}
          release={r}
          isLatest={idx === 0 || r.version === latest}
        />
      ))}
    </div>
  );
}

function ReleaseBlock({ release, isLatest }: { release: Release; isLatest: boolean }) {
  const items = isLatest ? splitNumberedItems(release.body) : null;
  return (
    <section
      className={
        "space-y-2 " +
        (isLatest
          ? "pt-1"
          : "pt-3 border-t border-[color:var(--midground-base)]/10")
      }
    >
      <h2 className={
        "font-serif tracking-tight " +
        (isLatest ? "text-xl" : "text-base opacity-80")
      }>
        v{release.version}
      </h2>
      {items && items.length > 0 ? (
        <>
          {items[0] && (
            <p className="text-sm leading-relaxed opacity-85">
              {renderInlineNotes(items[0])}
            </p>
          )}
          {items.length > 1 && (
            <ol className="text-sm leading-relaxed opacity-85 list-decimal pl-5 space-y-2 marker:opacity-50">
              {items.slice(1).map((it, i) => (
                <li key={i}>{renderInlineNotes(it)}</li>
              ))}
            </ol>
          )}
        </>
      ) : (
        <p className="text-sm leading-relaxed opacity-85">
          {renderInlineNotes(release.body)}
        </p>
      )}
    </section>
  );
}


/** Split a notes string on `vX.Y.Z:` boundaries (newest first). */
function splitReleases(notes: string): Release[] {
  const pattern = /v(\d+\.\d+(?:\.\d+)?):\s+/g;
  const matches: { version: string; index: number; len: number }[] = [];
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(notes)) !== null) {
    matches.push({ version: m[1], index: m.index, len: m[0].length });
  }
  if (matches.length === 0) return [];
  const out: Release[] = [];
  for (let i = 0; i < matches.length; i++) {
    const start = matches[i].index + matches[i].len;
    const end = i + 1 < matches.length ? matches[i + 1].index : notes.length;
    out.push({
      version: matches[i].version,
      body: notes.slice(start, end).trim(),
    });
  }
  return out;
}

/** Split the body on `(N)` numbered-item markers. First element is any
 *  preamble before `(1)`; subsequent elements are the items themselves. */
function splitNumberedItems(body: string): string[] {
  const parts = body.split(/\s*\((\d+)\)\s+/);
  // `split` with a capturing group: ["preamble", "1", "item1", "2", "item2", ...]
  if (parts.length <= 1) return [body];
  const out: string[] = [parts[0].trim()];
  for (let i = 2; i < parts.length; i += 2) out.push(parts[i].trim());
  return out.filter((s) => s.length > 0);
}

/** Render inline `code` spans as monospace chips. Matches OnboardingView. */
function renderInlineNotes(text: string): ReactNode {
  const parts: ReactNode[] = [];
  const pattern = /`([^`]+)`/g;
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = pattern.exec(text)) !== null) {
    if (m.index > lastIndex) {
      parts.push(text.slice(lastIndex, m.index));
    }
    parts.push(
      <code
        key={key++}
        className="text-[12px] font-mono px-1 py-0.5 rounded bg-[color:var(--midground-base)]/10"
      >
        {m[1]}
      </code>,
    );
    lastIndex = m.index + m[0].length;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return parts.length === 0 ? text : <>{parts}</>;
}


function ClaudeRunsPanel() {
  const [runs, setRuns] = useState<ClaudeRun[]>([]);
  const [summary, setSummary] = useState<SpendSummary | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [r, s] = await Promise.all([
        api.get<{ runs: ClaudeRun[] }>("/api/claude-runs?limit=20"),
        api.get<SpendSummary>("/api/claude-runs/summary?group_by=purpose").catch(() => null),
      ]);
      setRuns(r.runs);
      setSummary(s);
    } catch {
      setRuns([]);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  // Poll every 10s so the panel surfaces newly-completed runs while
  // the user is staring at the page (e.g. right after configuring a
  // new repo). Cheap — server reads at most RUN_LOG_LIMIT lines.
  useEffect(() => {
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [load]);

  if (!loaded) {
    return <div className="text-sm opacity-60">Loading…</div>;
  }
  if (runs.length === 0) {
    return (
      <div className="text-sm opacity-60">
        No PRISM-initiated claude executions yet. Configure a repo on a
        project, or click <span className="font-mono">Sync now</span> to
        trigger one.
      </div>
    );
  }

  const buckets = summary ? Object.entries(summary.buckets) : [];
  buckets.sort((a, b) => b[1].cost_usd - a[1].cost_usd);

  return (
    <div className="space-y-4">
      {buckets.length > 0 && (
        <div className="rounded-md border border-[color:var(--midground-base)]/10 p-3">
          <div className="flex items-baseline justify-between mb-2">
            <div className="text-2xs uppercase tracking-wider opacity-60">
              True spend by purpose
            </div>
            <div className="text-2xs opacity-50">measured from result-event usage</div>
          </div>
          <ul className="space-y-1 text-xs font-mono tabular-nums">
            {buckets.map(([purpose, b]) => (
              <li key={purpose} className="flex items-center gap-3">
                <span className="opacity-80 min-w-[10rem]">{purpose || "—"}</span>
                <span className="text-[color:var(--accent-emerald-fg)]">{_fmtUsd(b.cost_usd)}</span>
                <span className="opacity-50">{b.runs} run{b.runs === 1 ? "" : "s"}</span>
                <span className="opacity-50 ml-auto">
                  in {b.input_tokens.toLocaleString()} · out {b.output_tokens.toLocaleString()} · cache-r{" "}
                  {b.cache_read_input_tokens.toLocaleString()} · cache-w{" "}
                  {b.cache_creation_input_tokens.toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
          {summary && summary.prefix_runs > 0 && (
            <div className="text-2xs opacity-50 mt-2">
              {summary.prefix_runs} pre-fix run{summary.prefix_runs === 1 ? "" : "s"} excluded
              (recorded before the accounting fix — over-counted &amp; cache-blind, not mixed in).
            </div>
          )}
        </div>
      )}
    <ul className="divide-y divide-[color:var(--midground-base)]/10 -mx-2">
      {runs.map((r) => {
        const isExpanded = expandedId === r.run_id;
        const ok = r.exit_code === 0;
        return (
          <li key={r.run_id} className="px-2 py-2">
            <button
              type="button"
              onClick={() => setExpandedId(isExpanded ? null : r.run_id)}
              className="w-full text-left"
            >
              <div className="flex items-center gap-3 flex-wrap text-xs">
                <span
                  className={
                    "inline-flex items-center px-2 py-0.5 rounded-full uppercase tracking-wider text-2xs " +
                    (ok
                      ? "bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)]"
                      : "bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)]")
                  }
                  title={ok ? "exit 0" : `exit ${r.exit_code}`}
                >
                  {ok ? "ok" : `exit ${r.exit_code}`}
                </span>
                <span className="font-mono opacity-80">{r.purpose || "—"}</span>
                {r.project && (
                  <span className="opacity-60">
                    project <span className="font-mono">{r.project}</span>
                  </span>
                )}
                <span className="opacity-60">
                  {r.duration_s.toFixed(1)}s
                </span>
                <span className="opacity-60">
                  {r.tokens_used.toLocaleString()} tok
                </span>
                {typeof r.cost_usd === "number" && (
                  <span className="text-[color:var(--accent-emerald-fg)]" title="real per-run cost from the result event">
                    {_fmtUsd(r.cost_usd)}
                  </span>
                )}
                {!r.accounting_version && (
                  <span
                    className="text-2xs px-1.5 py-0.5 rounded bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]"
                    title="recorded before the accounting fix — token/cost figures over-counted and cache-blind"
                  >
                    pre-fix
                  </span>
                )}
                <span className="opacity-50 ml-auto text-2xs">
                  {new Date(r.ts_end * 1000).toLocaleString()}
                </span>
              </div>
            </button>
            {isExpanded && (
              <div className="mt-2 ml-2 space-y-2">
                {r.final_text ? (
                  <pre className="text-2xs whitespace-pre-wrap font-mono bg-[color:var(--midground-base)]/[0.04] border border-[color:var(--midground-base)]/10 rounded-md p-3 max-h-[400px] overflow-y-auto">
                    {r.final_text}
                  </pre>
                ) : (
                  <div className="text-2xs opacity-60">
                    No assistant text captured.
                  </div>
                )}
                {r.stderr_excerpt && (
                  <div>
                    <div className="text-2xs uppercase tracking-wider opacity-60 mb-1">
                      stderr
                    </div>
                    <pre className="text-2xs whitespace-pre-wrap font-mono bg-[color:var(--accent-rose-bg)] border border-[color:var(--accent-rose-ring)] rounded-md p-3 max-h-[200px] overflow-y-auto text-[color:var(--accent-rose-fg)]">
                      {r.stderr_excerpt}
                    </pre>
                  </div>
                )}
                <div className="text-2xs font-mono tabular-nums opacity-70 grid grid-cols-2 gap-x-4 gap-y-0.5 max-w-md">
                  <span>input <span className="opacity-100">{(r.input_tokens || 0).toLocaleString()}</span></span>
                  <span>output <span className="opacity-100">{(r.output_tokens || 0).toLocaleString()}</span></span>
                  <span>cache read <span className="opacity-100">{(r.cache_read_input_tokens || 0).toLocaleString()}</span></span>
                  <span>cache write <span className="opacity-100">{(r.cache_creation_input_tokens || 0).toLocaleString()}</span></span>
                  {typeof r.cost_usd === "number" && (
                    <span>cost <span className="text-[color:var(--accent-emerald-fg)]">{_fmtUsd(r.cost_usd)}</span></span>
                  )}
                  {r.model && <span>model <span className="opacity-100">{r.model}</span></span>}
                </div>
                <div className="text-2xs opacity-60">
                  run_id <span className="font-mono">{r.run_id}</span>
                  {" · "}
                  <a
                    href={`/api/claude-runs/${r.run_id}/stream`}
                    className="underline hover:no-underline"
                  >
                    download raw stream ({Math.round(r.stream_bytes / 1024)} KB)
                  </a>
                </div>
              </div>
            )}
          </li>
        );
      })}
    </ul>
    </div>
  );
}


function NewProjectRow({ onCreated }: { onCreated: (name: string) => void | Promise<void> }) {
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.post("/api/projects", { name: trimmed });
      setName("");
      await onCreated(trimmed);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} className="flex items-center gap-2 mb-2">
      <Plus className="w-4 h-4 opacity-60" />
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="new project name…"
        className="flex-1 px-2 py-1.5 rounded-md bg-transparent border-0 border-b border-dashed border-[color:var(--midground-base)]/20 text-sm font-mono focus:outline-none focus:border-[color:var(--midground-base)]/50"
      />
      <button
        type="submit"
        disabled={submitting || !name.trim()}
        className="px-3 py-1.5 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-2xs uppercase tracking-wider disabled:opacity-30"
      >
        {submitting ? <Loader2 className="w-3 h-3 animate-spin" /> : "Add"}
      </button>
      {error && <span className="text-2xs text-[color:var(--accent-rose-fg)]">{error}</span>}
    </form>
  );
}


function ProjectCard({
  name, info, isActive, onActivate, onSaved, onDeleted,
}: {
  name: string;
  info: ProjectInfo | undefined;
  isActive: boolean;
  onActivate: () => void;
  onSaved: () => void;
  onDeleted: () => Promise<void> | void;
}) {
  const [expanded, setExpanded] = useState(false);
  const busy = queueIsBusy(info?.queue);
  const drifted = isDrifted(info);

  // Auto-poll info every 5s while the queue is busy so the user sees
  // pending → in_progress → completed transitions without refreshing.
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => { onSaved(); }, 5000);
    return () => clearInterval(t);
  }, [busy, onSaved]);

  return (
    <li className="py-3">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-3 text-left group"
      >
        {expanded ? (
          <ChevronDown className="w-4 h-4 opacity-60" />
        ) : (
          <ChevronRight className="w-4 h-4 opacity-60" />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-[color:var(--midground-base)]">
              {name}
            </span>
            {isActive && (
              <span className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded-full bg-[color:var(--midground-base)]/15">
                active
              </span>
            )}
            <QueueBadge queue={info?.queue} />
            {drifted && !busy && (
              <span
                className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded-full bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]"
                title="The tracked ref has advanced past last_analyzed_sha — click to re-run analyzers."
              >
                drift
              </span>
            )}
          </div>
          <div className="text-2xs opacity-60 mt-1 flex flex-wrap gap-x-4 gap-y-1">
            {info?.mode === "folder" && info.source_path ? (
              <span className="inline-flex items-center gap-1 truncate max-w-[480px]">
                <FolderTree className="w-3 h-3" />
                <span className="font-mono">{info.source_path}</span>
              </span>
            ) : info?.mode === "clone" && info.remote_url ? (
              <>
                <span className="inline-flex items-center gap-1">
                  <GitBranch className="w-3 h-3" />
                  {info.tracked_ref ?? "origin/main"}
                </span>
                <span className="truncate max-w-[420px]">{info.remote_url}</span>
              </>
            ) : (
              <em className="opacity-60">no source</em>
            )}
            <span>
              sha: <span className="font-mono">
                {info?.current_sha ? info.current_sha.slice(0, 10) : "—"}
              </span>
            </span>
            <span>
              analyzed: <span className="font-mono">
                {info?.last_analyzed_sha ? info.last_analyzed_sha.slice(0, 10) : "—"}
              </span>
            </span>
          </div>
        </div>
      </button>
      {expanded && (
        <ProjectEditor
          name={name}
          info={info}
          onActivate={onActivate}
          onSaved={onSaved}
          onDeleted={onDeleted}
        />
      )}
    </li>
  );
}


/** Live scan-progress indicator. Polls /api/jobs and shows analyzers
 * enqueued for this project since `sinceTs`. While work is active,
 * renders a progress bar + the currently-running analyzer names; when
 * the queue drains, switches to a summary line. Caller drives lifecycle
 * via `sinceTs` (null = hidden) + `onDismiss`. */
function ScanProgress({
  project, sinceTs, onDismiss,
}: { project: string; sinceTs: number; onDismiss: () => void }) {
  // Used to run its own 2s/10s timeout loop with no hidden-tab skip; now
  // rides the ONE shared /api/jobs poller (task e8fc073b), which DOES
  // skip while the tab is hidden. The scoping filter is unchanged.
  const { jobs: allJobs } = useJobs();
  const jobs = allJobs.filter(
    (j) => j.project === project && j.enqueued_at >= sinceTs,
  );

  const counts = jobs.reduce(
    (acc, j) => { acc[j.state as keyof typeof acc] = (acc[j.state as keyof typeof acc] ?? 0) + 1; acc.total++; return acc; },
    { total: 0, pending: 0, in_progress: 0, completed: 0, failed: 0, cancelled: 0 } as Record<string, number>,
  );
  const done = counts.completed + counts.failed + counts.cancelled;
  const active = counts.pending + counts.in_progress;
  const total = counts.total;
  const allDone = active === 0 && total > 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  // Auto-dismiss the success summary after 12s so the editor doesn't
  // stay cluttered. Failures stick until the user dismisses (you want
  // to see those).
  useEffect(() => {
    if (!allDone || counts.failed > 0) return;
    const t = setTimeout(onDismiss, 12_000);
    return () => clearTimeout(t);
  }, [allDone, counts.failed, onDismiss]);

  const inProgressNames = jobs
    .filter((j) => j.state === "in_progress")
    .map((j) => j.analyzer)
    .join(", ");

  if (total === 0) {
    return (
      <div className="rounded-md border border-[color:var(--accent-emerald-ring)] bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] px-3 py-2 text-xs flex items-start gap-3">
        <Loader2 className="w-3.5 h-3.5 mt-0.5 shrink-0 animate-spin" />
        <span className="flex-1">Saved. Waiting for the analyzer queue to pick up the scan…</span>
        <button
          type="button"
          onClick={onDismiss}
          className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0"
        >
          dismiss
        </button>
      </div>
    );
  }

  if (allDone) {
    const tone = counts.failed > 0
      ? "border-[color:var(--accent-amber-ring)] bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)]"
      : "border-[color:var(--accent-emerald-ring)] bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)]";
    return (
      <div className={`rounded-md border px-3 py-2 text-xs flex items-start gap-3 ${tone}`}>
        <span className="flex-1">
          Scan complete: <strong>{counts.completed}</strong> done
          {counts.failed > 0 && <>, <strong className="text-[color:var(--accent-rose-fg)]">{counts.failed} failed</strong></>}
          {counts.cancelled > 0 && <>, {counts.cancelled} cancelled</>}
          .{" "}
          <span className="opacity-70">See the Jobs tab for details.</span>
        </span>
        <button
          type="button"
          onClick={onDismiss}
          className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0"
        >
          dismiss
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-[color:var(--accent-emerald-ring)] bg-[color:var(--accent-emerald-bg)] text-[color:var(--accent-emerald-fg)] px-3 py-2 text-xs space-y-2">
      <div className="flex items-center gap-3">
        <Loader2 className="w-3.5 h-3.5 shrink-0 animate-spin" />
        <span className="flex-1 font-mono tabular-nums">
          Scanning: <strong>{done}/{total}</strong> analyzers done · {pct}%
        </span>
        <button
          type="button"
          onClick={onDismiss}
          className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0"
        >
          dismiss
        </button>
      </div>
      <div className="h-1.5 rounded-full bg-[color:var(--background-base)]/40 overflow-hidden">
        <div
          className="h-full bg-[color:var(--accent-emerald-fg)] transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      {inProgressNames && (
        <div className="text-2xs opacity-75 font-mono">
          running: {inProgressNames}
        </div>
      )}
      {counts.failed > 0 && (
        <div className="text-2xs text-[color:var(--accent-rose-fg)]">
          {counts.failed} failed so far — see Jobs tab.
        </div>
      )}
    </div>
  );
}


function QueueBadge({ queue }: { queue: QueueCounts | undefined }) {
  if (!queue) return null;
  if (queue.in_progress > 0) {
    return (
      <span className="inline-flex items-center gap-1 text-2xs uppercase tracking-wider px-2 py-0.5 rounded-full bg-sky-500/15 text-sky-200">
        <Loader2 className="w-2.5 h-2.5 animate-spin" />
        analyzing {queue.in_progress}
      </span>
    );
  }
  if (queue.pending > 0) {
    return (
      <span className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded-full bg-indigo-500/15 text-indigo-200">
        {queue.pending} queued
      </span>
    );
  }
  if (queue.failed > 0) {
    return (
      <span className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded-full bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)]">
        {queue.failed} failed
      </span>
    );
  }
  return null;
}


function ProjectEditor({
  name, info, onActivate, onSaved, onDeleted,
}: {
  name: string;
  info: ProjectInfo | undefined;
  onActivate: () => void;
  onSaved: () => void;
  onDeleted: () => Promise<void> | void;
}) {
  // v5.2.0 — primary source mode is now "folder" (host path).
  // "url" is the legacy clone-mode preserved for backward compat. New
  // projects default to folder; existing projects default to whatever
  // they already are.
  const initialMode: "folder" | "url" =
    info?.mode === "clone" ? "url" :
    info?.mode === "folder" ? "folder" :
    // empty / new project — default to folder
    "folder";
  const [mode, setMode] = useState<"folder" | "url">(initialMode);
  const [runtime, setRuntime] = useState<"docker" | "native">("native");
  // v5.3.0 — default folder placeholder is runtime-aware: `/code/<name>`
  // for docker (the conventional bind-mount), empty for native (user
  // pastes any absolute host path).
  const [folderPath, setFolderPath] = useState(
    info?.source_path ?? "",
  );
  const [remote, setRemote] = useState(info?.remote_url ?? "");
  const [ref, setRef] = useState(info?.tracked_ref ?? "origin/main");
  const [submitting, setSubmitting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // unix seconds — when set, render <ScanProgress> live-polling /api/jobs
  // filtered to this project + only jobs enqueued at or after this time.
  const [scanStartedAt, setScanStartedAt] = useState<number | null>(null);
  const [ghAuthed, setGhAuthed] = useState(false);
  const [picking, setPicking] = useState(false);
  const isProtected = name === "default";

  useEffect(() => {
    setMode(
      info?.mode === "clone" ? "url" :
      info?.mode === "folder" ? "folder" :
      "folder"
    );
    setFolderPath(info?.source_path ?? (runtime === "docker" ? `/code/${name}` : ""));
    setRemote(info?.remote_url ?? "");
    setRef(info?.tracked_ref ?? "origin/main");
  }, [info?.mode, info?.source_path, info?.remote_url, info?.tracked_ref, name, runtime]);

  // Lazy-load github auth status so the Browse button only renders when
  // a repo list call would actually succeed. Cheap — single GET.
  useEffect(() => {
    api.get<GithubAuthStatus>("/api/github-auth/status")
      .then((s) => setGhAuthed(s.authenticated))
      .catch(() => setGhAuthed(false));
  }, []);

  // Resolve runtime once so the folder-mode hints + placeholder match
  // whether PRISM is running in docker or natively on the host.
  useEffect(() => {
    api.get<ServiceInfo>("/api/service-info")
      .then((s) => setRuntime(s.runtime ?? "native"))
      .catch(() => setRuntime("native"));
  }, []);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === "folder" && !folderPath.trim()) {
      setError(
        runtime === "docker"
          ? "Source path is required. Type the container-side path to the folder you bind-mounted into /code (see your docker-compose)."
          : "Source path is required. Paste the absolute path to your repo on this machine (e.g. C:\\Users\\you\\code\\my-repo or /Users/you/code/my-repo).",
      );
      return;
    }
    if (mode === "url" && !remote.trim()) {
      setError("git URL is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    // Stamp the moment we issued the save so <ScanProgress> only counts
    // jobs from this save forward (older completed/failed jobs from
    // previous scans don't pollute the progress bar).
    // Subtract 2s so server-side enqueue_at clock skew doesn't filter
    // out jobs that were actually triggered by this save.
    const startedAt = Math.floor(Date.now() / 1000) - 2;
    try {
      const body = mode === "folder"
        ? { source_path: folderPath.trim() }
        : { remote_url: remote.trim(), tracked_ref: ref.trim() || "origin/main" };
      await api.post(`/api/understand/configure?project=${encodeURIComponent(name)}`, body);
      setScanStartedAt(startedAt);
      onSaved();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSubmitting(false);
    }
  };

  const sync = async () => {
    setSyncing(true);
    setError(null);
    try {
      await api.post(`/api/understand/refresh?project=${encodeURIComponent(name)}`, {});
      onSaved();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSyncing(false);
    }
  };

  const remove = async () => {
    if (confirmText !== name) {
      setError(`Type "${name}" to confirm deletion.`);
      return;
    }
    setDeleting(true);
    setError(null);
    try {
      await api.delete(`/api/projects/${encodeURIComponent(name)}`);
      await onDeleted();
    } catch (e) {
      setError(String((e as Error).message ?? e));
      setDeleting(false);
    }
  };

  const drifted = isDrifted(info);
  const sourceConfigured = hasSource(info);

  return (
    <form
      onSubmit={save}
      className="mt-3 ml-7 rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--midground-base)]/[0.03] p-4 space-y-3"
    >
      {/* Source mode toggle — folder is the v5.2.0 primary path. */}
      <div className="flex items-center gap-1 text-2xs uppercase tracking-wider opacity-80">
        <span className="opacity-60 mr-2">Source</span>
        <button
          type="button"
          onClick={() => setMode("folder")}
          className={
            "px-3 py-1 rounded-md border " +
            (mode === "folder"
              ? "bg-[color:var(--midground-base)]/15 border-[color:var(--midground-base)]/40"
              : "border-[color:var(--midground-base)]/15 opacity-70 hover:opacity-100")
          }
        >
          <FolderTree className="w-3 h-3 inline mr-1 -mt-0.5" />
          Local folder
        </button>
        <button
          type="button"
          onClick={() => setMode("url")}
          className={
            "px-3 py-1 rounded-md border " +
            (mode === "url"
              ? "bg-[color:var(--midground-base)]/15 border-[color:var(--midground-base)]/40"
              : "border-[color:var(--midground-base)]/15 opacity-70 hover:opacity-100")
          }
        >
          <GitBranch className="w-3 h-3 inline mr-1 -mt-0.5" />
          Git URL
        </button>
        <span className="opacity-50 normal-case ml-auto">
          {mode === "folder"
            ? "PRISM reads files where they already live on your host"
            : "PRISM clones the repo server-side"}
        </span>
      </div>

      {mode === "folder" ? (
        <div className="space-y-2">
          <label className="flex flex-col gap-1 min-w-0">
            <span className="text-2xs uppercase tracking-wider opacity-60">
              {runtime === "docker" ? "Container path" : "Folder path"}
            </span>
            <div className="flex gap-2 min-w-0">
              <input
                value={folderPath}
                onChange={(e) => setFolderPath(e.target.value)}
                placeholder={
                  runtime === "docker"
                    ? "/code/your-repo"
                    : "C:\\Users\\you\\code\\my-repo  •  /Users/you/code/my-repo"
                }
                className="flex-1 min-w-0 px-3 py-2 rounded-md bg-[color:var(--background-base)]/60 border border-[color:var(--midground-base)]/20 text-sm font-mono"
              />
              {isInTauri() && runtime !== "docker" && (
                <button
                  type="button"
                  onClick={async () => {
                    setError(null);
                    try {
                      const { open } = await import("@tauri-apps/plugin-dialog");
                      const picked = await open({
                        directory: true,
                        multiple: false,
                        title: "Pick a project folder",
                        defaultPath: folderPath || undefined,
                      });
                      if (typeof picked === "string" && picked) {
                        setFolderPath(picked);
                      }
                    } catch (e) {
                      setError(`Folder picker failed: ${(e as Error).message ?? e}`);
                    }
                  }}
                  className="shrink-0 px-3 py-2 rounded-md border border-[color:var(--midground-base)]/30 text-xs uppercase tracking-wider hover:bg-[color:var(--midground-base)]/10"
                >
                  Browse…
                </button>
              )}
            </div>
          </label>
          <p className="text-2xs opacity-60 leading-snug">
            {runtime === "docker" ? (
              <>
                This path lives <em>inside the container</em>. Bind-mount
                your code dir into{" "}
                <span className="font-mono">/code</span> in docker-compose
                (default is <span className="font-mono">~/code</span> on
                the host), then point at a subfolder here. No git URL, no
                auth, no clone.
              </>
            ) : (
              <>
                Absolute path to your repo on this machine. PRISM reads
                files where they already live — no clone, no auth needed.
                Works with anything <span className="font-mono">git</span>{" "}
                can read (commits, branches, drift detection) and anything
                it can't (loose folders, notes dirs).
              </>
            )}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-[1fr_180px] gap-3">
            <label className="flex flex-col gap-1 min-w-0">
              <span className="text-2xs uppercase tracking-wider opacity-60 flex items-center gap-2">
                Git URL
                {ghAuthed && (
                  <button
                    type="button"
                    onClick={() => setPicking(true)}
                    className="ml-auto inline-flex items-center gap-1 text-2xs uppercase tracking-wider opacity-80 hover:opacity-100"
                  >
                    <Github className="w-3 h-3" /> Browse my repos
                  </button>
                )}
              </span>
              <input
                value={remote}
                onChange={(e) => setRemote(e.target.value)}
                placeholder="https://github.com/owner/repo"
                className="px-3 py-2 rounded-md bg-[color:var(--background-base)]/60 border border-[color:var(--midground-base)]/20 text-sm font-mono"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-2xs uppercase tracking-wider opacity-60">
                Tracked ref
              </span>
              <input
                value={ref}
                onChange={(e) => setRef(e.target.value)}
                className="px-3 py-2 rounded-md bg-[color:var(--background-base)]/60 border border-[color:var(--midground-base)]/20 text-sm font-mono"
              />
            </label>
          </div>
          {/* GitHub connect, surfaced contextually only when the user is in
              Git URL mode and isn't yet authenticated. If they only ever use
              folder mode, they never see this. */}
          {!ghAuthed && (
            <details className="rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--midground-base)]/[0.02]">
              <summary className="px-3 py-2 text-2xs cursor-pointer flex items-center gap-2 hover:bg-[color:var(--midground-base)]/[0.04]">
                <Github className="w-3.5 h-3.5 opacity-70" />
                <span className="opacity-80">
                  Optional: connect GitHub for private-repo clones + a repo picker
                </span>
              </summary>
              <div className="px-3 pt-2 pb-3 border-t border-[color:var(--midground-base)]/10">
                <GithubAuthCard />
              </div>
            </details>
          )}
        </div>
      )}
      {mode === "url" && info?.remote_url && info.remote_url !== remote.trim() && (
        <div className="text-2xs opacity-60">
          Changing the URL is refused server-side once a clone exists — to
          re-point, delete the project's source/ dir first.
        </div>
      )}
      {error && (
        <div className="rounded-md border border-[color:var(--accent-rose-ring)] bg-[color:var(--accent-rose-bg)] text-[color:var(--accent-rose-fg)] px-3 py-2 text-xs">
          {error}
        </div>
      )}
      {scanStartedAt !== null && (
        <ScanProgress
          project={name}
          sinceTs={scanStartedAt}
          onDismiss={() => setScanStartedAt(null)}
        />
      )}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <button
          type="button"
          onClick={onActivate}
          className="text-2xs uppercase tracking-wider opacity-70 hover:opacity-100"
        >
          Make active
        </button>
        <div className="flex items-center gap-2">
          {sourceConfigured && (
            <button
              type="button"
              onClick={sync}
              disabled={syncing || submitting || deleting}
              title={drifted
                ? "Re-run analyzers against the latest commit"
                : "Re-run analyzers (no drift detected)"}
              className="inline-flex items-center gap-2 px-3 py-2 rounded-md border border-[color:var(--midground-base)]/30 text-xs uppercase tracking-wider disabled:opacity-40 hover:bg-[color:var(--midground-base)]/10"
            >
              {syncing && <Loader2 className="w-3 h-3 animate-spin" />}
              {syncing ? "Syncing…" : "Sync now"}
            </button>
          )}
          <button
            type="submit"
            disabled={submitting || deleting}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-xs uppercase tracking-wider disabled:opacity-40"
          >
            {submitting && <Loader2 className="w-3 h-3 animate-spin" />}
            {submitting ? "Saving…" : sourceConfigured ? "Update source" : "Set source"}
          </button>
        </div>
      </div>

      {!isProtected && (
        <div className="pt-3 border-t border-[color:var(--midground-base)]/10">
          {!confirming ? (
            <button
              type="button"
              onClick={() => { setConfirming(true); setError(null); }}
              className="text-2xs uppercase tracking-wider text-[color:var(--accent-rose-fg)] hover:text-[color:var(--accent-rose-fg)]"
            >
              Delete project + all data
            </button>
          ) : (
            <div className="space-y-2">
              <div className="text-2xs text-[color:var(--accent-rose-fg)]">
                This wipes <span className="font-mono">data/projects/{name}</span>:
                brain, graph, tasks, scores, source clone, queue, all artifacts.
                Type <span className="font-mono">{name}</span> to confirm.
              </div>
              <div className="flex items-center gap-2">
                <input
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder={name}
                  disabled={deleting}
                  className="flex-1 px-3 py-2 rounded-md bg-[color:var(--background-base)]/60 border border-[color:var(--accent-rose-ring)] text-sm font-mono"
                />
                <button
                  type="button"
                  onClick={remove}
                  disabled={deleting || confirmText !== name}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-[color:var(--accent-rose-fg)] text-white text-xs uppercase tracking-wider disabled:opacity-30"
                >
                  {deleting && <Loader2 className="w-3 h-3 animate-spin" />}
                  {deleting ? "Deleting…" : "Delete forever"}
                </button>
                <button
                  type="button"
                  onClick={() => { setConfirming(false); setConfirmText(""); }}
                  disabled={deleting}
                  className="text-2xs uppercase tracking-wider opacity-70 hover:opacity-100"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
      {picking && (
        <RepoPickerModal
          onClose={() => setPicking(false)}
          onPick={(url, defaultBranch) => {
            setRemote(url);
            // Only auto-fill the tracked ref when the user hasn't
            // customized it from the default — otherwise we'd clobber
            // their choice on every pick.
            if (!ref || ref === "origin/main") {
              setRef(`origin/${defaultBranch}`);
            }
            setPicking(false);
          }}
        />
      )}
    </form>
  );
}

// Integration setup + manual sync (task ae31c2c0). Lists the workspace's
// GitHub/Jira connections and their containers, triggers a manual pull, and
// links the durable sanitized receipt the sync produced. Authorization and
// receipts are the server's — this card only surfaces them.
// IntegrationsCard was removed with task 900a4fb9. It rendered the
// repo picker behind a TEAM WORKSPACE, which a local install never
// creates, so it always showed "No team workspace yet". RepoSync
// above replaces it on the personal scope.

/** Syncing is a CHOICE, separate from connecting. It starts off, and a
 *  working credential never turns it on (owner 2026-07-28, task 01118728). */
function SyncSwitch({ connector, noun, onChanged }: { connector: Connector; noun: string; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const on = connector.sync_enabled === true;

  const flip = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    setBusy(true);
    try {
      await setConnectorSync(connector.provider, !on);
      onChanged();
    } finally { setBusy(false); }
  }, [connector.provider, on, onChanged]);

  return (
    <div className="flex items-start justify-between gap-4 pb-4">
      <div className="min-w-0">
        <div className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
          Sync PRISM tasks with {connector.name} {noun}
        </div>
        <p className="text-xs mt-1" style={{ color: "var(--text-muted)" }}>
          {on
            ? `On. PRISM tasks and ${connector.name} ${noun} are kept in step with each other. Your PRISM tasks stay the record either way.`
            : `Off. Your PRISM tasks are untouched and no ${connector.name} ${noun} are read or written. PRISM tracks this work on its own, so leaving this off costs you nothing.`}
        </p>
      </div>
      <button type="button" role="switch" aria-checked={on} disabled={busy}
        onClick={flip}
        title={on
          ? `Stop syncing PRISM tasks with ${connector.name} ${noun}`
          : `Sync PRISM tasks with ${connector.name} ${noun}`}
        className={cn(
          "shrink-0 w-11 h-6 rounded-full relative transition-colors disabled:opacity-40",
          on ? "bg-[color:var(--accent-sage-fg)]" : "bg-[color:var(--surface-3)] border border-[color:var(--border-default)]",
        )}>
        <span className={cn(
          "absolute top-1 w-4 h-4 rounded-full bg-white transition-all",
          on ? "left-6" : "left-1",
        )} />
      </button>
    </div>
  );
}

/** Choose a repository and pull its issues in as PRISM tasks. No team
 *  workspace needed: this is the local, personal scope (task 900a4fb9). */
// Does a task created in PRISM actually turn into an issue? (task 27e543e0)
//
// This line exists because the answer used to be "no" while every surface
// said "Connected": the push existed, nothing called it, and there was
// nowhere a person could look to find that out. So it never renders a bare
// "off" — when the mirror is not live it names the ONE link that is missing,
// which is the difference between a status and a diagnosis.
function MirrorLine({ connector }: { connector: Connector }) {
  const [mirror, setMirror] = useState<MirrorStatus | null>(null);
  useEffect(() => {
    let alive = true;
    getMirrorStatus().then((m) => { if (alive) setMirror(m); }).catch(() => {});
    return () => { alive = false; };
  }, [connector.sync_enabled, connector.tracking?.length]);

  if (!mirror) return null;
  const missing =
    !mirror.enabled ? `${mirror.env} is turned off`
    : !mirror.observer_installed ? "PRISM has not finished starting up"
    : !mirror.adapters.includes("github")
      ? (mirror.adapter_errors.github ?? "the GitHub adapter did not load")
    : !mirror.sync_enabled ? "syncing is turned off above"
    : mirror.tracking.length === 0 ? "no repository is tracked yet"
    : "";

  return (
    <div>
      <SectionLabel>New tasks</SectionLabel>
      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        {mirror.ready ? (
          <>Every task you create here becomes an issue in{" "}
            <span className="font-mono" style={{ color: "var(--text-secondary)" }}>
              {mirror.tracking.join(", ")}
            </span>, and the task links back to it. Tasks that already existed
            stay local until you push them.</>
        ) : (
          <>New tasks are not reaching GitHub yet, because {missing}.</>
        )}
      </p>
    </div>
  );
}

function RepoSync({ connector, project, onChanged }:
  { connector: Connector; project: string; onChanged: () => void }) {
  const [repo, setRepo] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const tracked = connector.tracking ?? [];

  // Jira tracks a bare PROJECT KEY (task 64ba4755, FR-4), never owner/repo.
  const isJira = connector.provider === "jira";

  const track = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    setErr(""); setNote(""); setBusy("track");
    try {
      await trackConnectorRepo(connector.provider, repo.trim());
      setRepo(""); onChanged();
    } catch (ex) { setErr(String((ex as Error).message ?? ex)); }
    finally { setBusy(""); }
  }, [connector.provider, repo, onChanged]);

  const sync = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    setErr(""); setNote(""); setBusy("sync");
    try {
      const r = await runConnectorSync(connector.provider, project);
      setNote(`Imported ${r.imported} item${r.imported === 1 ? "" : "s"} as PRISM tasks.`);
      onChanged();
    } catch (ex) { setErr(String((ex as Error).message ?? ex)); }
    finally { setBusy(""); }
  }, [connector.provider, project, onChanged]);

  return (
    <div className="space-y-3" onClick={(e) => e.stopPropagation()}>
      <div>
        <SectionLabel>{isJira ? "Projects" : "Repositories"}</SectionLabel>
        {tracked.length === 0 ? (
          <p className="text-xs" style={{ color: "var(--text-muted)" }}>
            Nothing tracked yet. Add one as {isJira ? "a project key" : "owner/repo"}.
          </p>
        ) : (
          <ul className="text-xs font-mono" style={{ color: "var(--text-secondary)" }}>
            {tracked.map((t) => <li key={t.key}>{t.key}</li>)}
          </ul>
        )}
      </div>
      {connector.provider === "github" && <MirrorLine connector={connector} />}
      <div className="flex items-center gap-2">
        <input value={repo} onChange={(e) => setRepo(e.target.value)}
          placeholder={isJira ? "KEY" : "owner/repo"}
          className="flex-1 text-xs font-mono px-3 py-1.5 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)]" />
        <button type="button" onClick={track} disabled={!repo.trim() || busy !== ""}
          className="text-xs font-semibold px-3 py-1.5 rounded-md border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40">
          Track
        </button>
        <button type="button" onClick={sync} disabled={busy !== "" || tracked.length === 0}
          className="text-xs font-semibold px-3 py-1.5 rounded-md border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
          style={{ color: "var(--accent-teal-fg)" }}>
          {busy === "sync" ? "Syncing…" : "Sync now"}
        </button>
      </div>
      {connector.last_sync && (
        <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
          {connector.last_sync.imported === 0 ? (
            <>Last sync found nothing new from {connector.last_sync.container}
              {connector.last_sync.reason ? `: ${connector.last_sync.reason}.` : "."}</>
          ) : (
            <>Last sync imported {connector.last_sync.imported} item
              {connector.last_sync.imported === 1 ? "" : "s"} from {connector.last_sync.container}.{" "}
              <Link to={`/tasks?q=${encodeURIComponent(connector.provider)}`}
                className="underline" style={{ color: "var(--accent-teal-fg)" }}>
                View in Work
              </Link></>
          )}
        </p>
      )}
      {note && <p className="text-xs" style={{ color: "var(--accent-sage-fg)" }}>{note}</p>}
      {err && <p className="text-xs" style={{ color: "var(--accent-amber-fg)" }}>{err}</p>}
    </div>
  );
}

/** Turn the backlog that predates syncing into GitHub issues (task
 *  733af05f). Two explicit steps, never a silent sweep: preview names what
 *  it WOULD do and touches nothing; confirm is a second, separate click. */
function BacklogPush({ connector, project }: { connector: Connector; project: string }) {
  const [report, setReport] = useState<BacklogPushReport | null>(null);
  const [done, setDone] = useState<BacklogPushReport | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const preview = useCallback(async () => {
    setErr(""); setDone(null); setBusy("preview");
    try {
      setReport(await pushBacklog(connector.provider, project, { dryRun: true }));
    } catch (ex) { setErr(String((ex as Error).message ?? ex)); }
    finally { setBusy(""); }
  }, [connector.provider, project]);

  const confirm = useCallback(async () => {
    if (!report) return;
    setErr(""); setBusy("confirm");
    try {
      const r = await pushBacklog(connector.provider, project,
        { dryRun: false, taskIds: report.would_create });
      setDone(r); setReport(null);
    } catch (ex) { setErr(String((ex as Error).message ?? ex)); }
    finally { setBusy(""); }
  }, [connector.provider, project, report]);

  return (
    <div className="mt-4 pt-4 border-t border-[color:var(--border-subtle)]" onClick={(e) => e.stopPropagation()}>
      <SectionLabel>Existing backlog</SectionLabel>
      <p className="text-xs mb-2" style={{ color: "var(--text-muted)" }}>
        Tasks that existed before you turned syncing on stay local until you push them.
      </p>
      <button type="button" onClick={preview} disabled={busy !== ""}
        className="text-xs font-semibold px-3 py-1.5 rounded-md border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40">
        {busy === "preview" ? "Checking…" : "Preview backlog push"}
      </button>
      {report && (
        <div className="mt-2 text-xs space-y-1" style={{ color: "var(--text-secondary)" }}>
          <p>Would create <b>{report.would_create.length}</b> issue{report.would_create.length === 1 ? "" : "s"}.</p>
          <p style={{ color: "var(--text-muted)" }}>
            Skipping {report.skipped.done.length} done, {report.skipped.cancelled.length} cancelled,{" "}
            {report.skipped.already_linked.length} already linked.
          </p>
          {report.would_create.length > 0 ? (
            <button type="button" onClick={confirm} disabled={busy !== ""}
              className="text-xs font-semibold px-3 py-1.5 rounded-md border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
              style={{ color: "var(--accent-teal-fg)" }}>
              {busy === "confirm" ? "Pushing…" : `Confirm: push ${report.would_create.length} to ${connector.name}`}
            </button>
          ) : <p>Nothing to push.</p>}
        </div>
      )}
      {done && (
        <p className="mt-2 text-xs" style={{ color: "var(--accent-sage-fg)" }}>
          {`Created ${done.created.length} issue${done.created.length === 1 ? "" : "s"} on ${connector.name}.`}
        </p>
      )}
      {err && <p className="mt-2 text-xs" style={{ color: "var(--accent-amber-fg)" }}>{err}</p>}
    </div>
  );
}

/** Connect Jira with a site URL + email + Atlassian API token (task
 *  64ba4755) — this instance has no Atlassian OAuth app registered, so the
 *  OAuth "Connect" button above has nothing to authorize against. Shown in
 *  BOTH the not_connected and not_configured branches: an unconfigured
 *  OAuth app must never be the last word once this path can connect on its
 *  own. The raw token is never echoed back; only the validated account name. */
function JiraApiTokenForm({ onConnected }: { onConnected: () => void }) {
  const [siteUrl, setSiteUrl] = useState("");
  const [email, setEmail] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const connect = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(""); setBusy(true);
    try {
      await connectJiraApiToken(siteUrl.trim(), email.trim(), token);
      setToken("");
      onConnected();
    } catch (ex) { setErr(String((ex as Error).message ?? ex)); }
    finally { setBusy(false); }
  }, [siteUrl, email, token, onConnected]);

  return (
    <form className="space-y-2" onSubmit={connect} onClick={(e) => e.stopPropagation()}>
      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        Connect with an{" "}
        <a href="https://id.atlassian.com/manage-profile/security/api-tokens"
          target="_blank" rel="noopener" className="underline">
          Atlassian API token
        </a>{" "}— no admin-installed app needed on this instance.
      </p>
      <input value={siteUrl} onChange={(e) => setSiteUrl(e.target.value)}
        placeholder="https://your-site.atlassian.net"
        className="w-full text-xs font-mono px-3 py-1.5 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)]" />
      <input value={email} onChange={(e) => setEmail(e.target.value)}
        placeholder="you@example.com" type="email"
        className="w-full text-xs font-mono px-3 py-1.5 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)]" />
      <input value={token} onChange={(e) => setToken(e.target.value)}
        placeholder="API token" type="password"
        className="w-full text-xs font-mono px-3 py-1.5 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)]" />
      <button type="submit"
        disabled={busy || !siteUrl.trim() || !email.trim() || !token}
        className="text-xs font-semibold px-3 py-1.5 rounded-md border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
        style={{ color: "var(--accent-teal-fg)" }}>
        {busy ? "Connecting…" : "Connect Jira"}
      </button>
      {err && <p className="text-xs" style={{ color: "var(--accent-amber-fg)" }}>{err}</p>}
    </form>
  );
}

function ConnectorsSection({ project }: { project: string }) {
  const [rows, setRows] = useState<Connector[]>([]);
  const [err, setErr] = useState<string>("");
  const [busy, setBusy] = useState<string>("");
  // Which card has its detail open. A collapsed box is a LAZY LOAD (owner
  // rule): the detail's children only mount once it is opened, so their
  // polling never runs for a panel nobody looked at.
  const [openDetail, setOpenDetail] = useState<string>("");

  const reload = useCallback(() => {
    listConnectorStatus().then(setRows).catch((e) => setErr(String((e as Error).message ?? e)));
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const connect = useCallback(async (provider: string) => {
    setErr(""); setBusy(provider);
    try {
      const url = await startConnect(provider);
      window.open(url, "_blank", "noopener");
    } catch (e) {
      setErr(String((e as Error).message ?? e));
    } finally { setBusy(""); }
  }, []);

  const TONE: Record<string, string> = {
    connected: "var(--accent-sage-fg)",
    needs_attention: "var(--accent-amber-fg)",
    not_connected: "var(--text-muted)",
    not_configured: "var(--text-disabled)",
  };
  // What this provider CALLS the work it holds. Its presence is also the
  // capability answer: a connector with no work noun has nothing to sync and
  // gets no sync switch. Claude falls out here because it is a credential for
  // running analyzers, not a tracker, never because it is named Claude
  // (task fc6ec2c9). The server agrees: PROVIDERS is ("github", "jira").
  const WORK_NOUN: Record<string, string> = {
    github: "issues",
    jira: "issues",
  };
  const LABEL: Record<string, string> = {
    connected: "Connected",
    needs_attention: "Connection issue",
    not_connected: "Not connected",
    not_configured: "Not configured",
  };

  return (
    <div className="space-y-4">
      {err && <ErrorBanner>{err}</ErrorBanner>}
      {rows.length === 0 && <Empty>Loading connectors…</Empty>}
      {rows.map((c) => {
      const isOpen = openDetail === c.provider;
      const toggle = () => setOpenDetail(isOpen ? "" : c.provider);
      return (
        // p-0 hands the padding to the header below, so the ENTIRE panel is
        // the click target rather than a small button in the corner
        // (owner 2026-07-28). twMerge lets p-0 win over Card's own p-5.
        <Card key={c.provider} className="p-0 overflow-hidden">
          <div
            role="button"
            tabIndex={0}
            aria-expanded={isOpen}
            onClick={toggle}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
            }}
            className="p-5 cursor-pointer transition-colors hover:bg-[color:var(--surface-2)] flex items-start gap-3"
          >
            <span
              className="shrink-0 grid place-items-center w-9 h-9 rounded-md font-semibold text-sm"
              style={{ background: "var(--surface-2)", color: "var(--text-secondary)" }}
              aria-hidden
            >
              {c.name.slice(0, 2)}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-semibold" style={{ color: "var(--text-primary)" }}>{c.name}</span>
              </div>
              {c.detail && (
                <p className="text-xs mt-1" style={{ color: "var(--text-muted)" }}>{c.detail}</p>
              )}
              {c.account && (
                <p className="text-2xs mt-1 font-mono" style={{ color: "var(--text-secondary)" }}>{c.account}</p>
              )}
              {c.tracking && c.tracking.length > 0 && (
                <p className="text-2xs mt-1 space-x-2" style={{ color: "var(--text-muted)" }}>
                  <span>Tracking:</span>
                  {c.tracking.map((t) => (
                    <a key={t.key} href={t.url} target="_blank" rel="noreferrer"
                      onClick={function stopPropagationOnly(e) { e.stopPropagation(); }}
                      className="underline decoration-dotted hover:decoration-solid"
                      style={{ color: "var(--text-secondary)" }}>
                      {t.key}
                    </a>
                  ))}
                </p>
              )}
            </div>
            <div className="shrink-0 flex items-center gap-2">
              {/* The state badge IS the configuration control, top right
                  (owner 2026-07-28). stopPropagation first: it sits inside
                  the card's own clickable header, so without it a click
                  would toggle the card twice and land on the wrong state. */}
              <button type="button"
                onClick={(e) => { e.stopPropagation(); setOpenDetail(isOpen ? "" : c.provider); }}
                aria-expanded={isOpen}
                title={`Configure ${c.name}`}
                className="text-2xs uppercase tracking-wider font-semibold px-2 py-1 rounded-md border border-transparent hover:border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] transition-colors"
                style={{ color: TONE[c.state] ?? "var(--text-muted)" }}>
                {LABEL[c.state] ?? c.state}
              </button>
              {c.state === "needs_attention" && (
                <button type="button" disabled={busy === c.provider}
                  onClick={(e) => { e.stopPropagation(); connect(c.provider); }}
                  className="text-xs font-semibold px-3 py-1.5 rounded-md border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
                  style={{ color: "var(--accent-amber-fg)" }}>
                  Reconnect
                </button>
              )}
              {c.state === "not_connected" && (
                <button type="button" disabled={busy === c.provider}
                  onClick={(e) => { e.stopPropagation(); connect(c.provider); }}
                  className="text-xs font-semibold px-3 py-1.5 rounded-md border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
                  style={{ color: "var(--accent-teal-fg)" }}>
                  Connect {c.name}
                </button>
              )}
              {/* The card's own state, shown where a Details button used to
                  be. The whole panel is the control now. */}
              <ChevronDown
                aria-hidden
                className={cn("w-4 h-4 transition-transform", isOpen && "rotate-180")}
                style={{ color: "var(--text-muted)" }}
              />
            </div>
          </div>
          {/* Claude's own detail. It used to be the standalone Claude auth
              page; it belongs to Claude's card, not to a second nav entry.
              Guarded by openDetail so ClaudeAuthCard's 5s status poll starts
              on expand, never on page load (task c89edbeb). */}
          {openDetail === c.provider && WORK_NOUN[c.provider] && (
            <div className="px-5 pt-4 border-t border-[color:var(--border-subtle)]">
              <SyncSwitch connector={c} noun={WORK_NOUN[c.provider]} onChanged={reload} />
            </div>
          )}
          {c.provider === "claude" && openDetail === c.provider && (
            <div className="px-5 pb-5 pt-4 space-y-4">
              <div>
                <SectionLabel>Claude usage</SectionLabel>
                <ClaudeUsageCard />
              </div>
              <div>
                <SectionLabel>Claude auth</SectionLabel>
                <ClaudeAuthCard />
              </div>
              <div>
                <SectionLabel>Claude source</SectionLabel>
                <ClaudeSourceCard project={project} />
              </div>
            </div>
          )}
          {c.provider !== "claude" && openDetail === c.provider && (
            <div className="px-5 pb-5 pt-4">
              {c.state === "connected" ? (
                <>
                  <RepoSync connector={c} project={project} onChanged={reload} />
                  {(c.provider === "github" || c.provider === "jira") &&
                    (c.tracking?.length ?? 0) > 0 && (
                    <BacklogPush connector={c} project={project} />
                  )}
                </>
              ) : (
                <div className="space-y-3">
                  <p className="text-xs" style={{ color: "var(--text-muted)" }}>
                    {c.detail} Connecting is optional: PRISM tracks your work
                    on its own, and {c.name} only adds a place to see work
                    that already lives there.
                  </p>
                  {c.provider === "jira"
                    && (c.state === "not_connected" || c.state === "not_configured")
                    && <JiraApiTokenForm onConnected={reload} />}
                </div>
              )}
            </div>
          )}
        </Card>
      );
      })}
      <CollaborationConnectorCard />
    </div>
  );
}

// A DIFFERENT registry from the provider rows above: not "sync issues from
// this provider" but "reach a person who is not at this machine to decide
// a gate" (services/collaboration.py). Reads the live registry - a surface
// renders only when production code actually registered or attempted it,
// never a fixture standing in for one (task f4dd3687's whole point).
function CollaborationConnectorCard() {
  const [rows, setRows] = useState<CollaborationSurface[]>([]);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    listCollaborationSurfaces().then(setRows)
      .catch((e) => setErr(String((e as Error).message ?? e)));
  }, []);

  const NAME: Record<string, string> = { github: "GitHub" };
  const TONE: Record<string, string> = {
    connected: "var(--accent-sage-fg)",
    unavailable: "var(--accent-amber-fg)",
  };
  const LABEL: Record<string, string> = {
    connected: "Connected",
    unavailable: "Unavailable",
  };

  return (
    <Card className="p-5">
      <div className="flex items-start gap-3">
        <span
          className="shrink-0 grid place-items-center w-9 h-9 rounded-md font-semibold text-sm"
          style={{ background: "var(--surface-2)", color: "var(--text-secondary)" }}
          aria-hidden
        >
          Co
        </span>
        <div className="min-w-0 flex-1">
          <span className="font-semibold" style={{ color: "var(--text-primary)" }}>
            Collaboration
          </span>
          <p className="text-xs mt-1" style={{ color: "var(--text-muted)" }}>
            Where a gate can reach someone who is not at this machine.
          </p>
          {err && <ErrorBanner>{err}</ErrorBanner>}
          {!err && rows.length === 0 && (
            <p className="text-2xs mt-2" style={{ color: "var(--text-muted)" }}>
              Not configured. Nothing has registered a surface here yet.
            </p>
          )}
          {rows.map((r) => (
            <div key={r.surface} className="flex items-center gap-2 mt-2">
              <span className="text-xs font-semibold" style={{ color: "var(--text-primary)" }}>
                {NAME[r.surface] ?? r.surface}
              </span>
              <span
                className="text-2xs uppercase tracking-wider font-semibold"
                style={{ color: TONE[r.state] ?? "var(--text-muted)" }}
              >
                {LABEL[r.state] ?? r.state}
              </span>
              {r.state === "unavailable" && r.detail && (
                <span className="text-2xs" style={{ color: "var(--text-muted)" }}>
                  {r.detail}
                </span>
              )}
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}
