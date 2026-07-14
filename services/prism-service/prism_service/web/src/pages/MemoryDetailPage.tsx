import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import {
  Page, Card, SectionLabel, Empty, ErrorBanner, type PillTone,
} from "@/components/ui";
import { domainTone, importanceTone } from "@/lib/domainTone";
import EvidenceView from "@/components/EvidenceView";

type Entry = {
  id?: string;
  name?: string;
  type?: string;
  classification?: string;
  status?: string;
  description?: string;
  summary?: string;
  domain?: string;
  importance?: number;
  memory_type?: string;
  recall_count?: number;
  last_recalled?: string;
  effectiveness?: number;
  valid_at?: string;
  invalid_at?: string;
  generation?: number;
  recorded_at?: string;
  evidence?: Record<string, unknown>;
};

function shortDate(iso?: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
    return d.toLocaleDateString() + " " + d.toLocaleTimeString();
  } catch {
    return iso.slice(0, 10);
  }
}

export default function MemoryDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [project] = useProject();
  const [entry, setEntry] = useState<Entry | null>(null);
  const [chain, setChain] = useState<Entry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  // edit / retire / supersede affordances, backed by POST
  // /api/memory/entry/:id/action (the same store/update path memory_store uses).
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [actionNote, setActionNote] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const load = () => api.get<{ entry: Entry; chain: Entry[] }>(
      `/api/memory/entry/${encodeURIComponent(id)}?project=${project}`,
    )
      .then((d) => {
        if (cancelled) return;
        setEntry(d.entry); setChain(d.chain ?? []); setError(null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String((e as Error).message ?? e));
        setEntry(null); setChain([]);
      })
      .finally(() => { if (!cancelled) setLoaded(true); });
    load();
    // Re-poll while the summary is still being filled so the page
    // updates the moment the worker writes back.
    const t = setInterval(() => {
      if (entry && !entry.summary) load();
    }, 10000);
    return () => { cancelled = true; clearInterval(t); };
  }, [id, project, entry]);

  if (!loaded) {
    return <Page><div className="text-sm opacity-60">Loading…</div></Page>;
  }
  if (error) {
    return (
      <Page>
        <BackLink onClick={() => navigate("/memory")} />
        <ErrorBanner>{error}</ErrorBanner>
      </Page>
    );
  }
  if (!entry) {
    return (
      <Page>
        <BackLink onClick={() => navigate("/memory")} />
        <Empty>Memory not found.</Empty>
      </Page>
    );
  }

  const doAction = async (
    action: "edit" | "retire" | "supersede",
    payload: Record<string, unknown> = {},
  ) => {
    setActionBusy(true);
    try {
      const r = await fetch(
        `/api/memory/entry/${encodeURIComponent(id)}/action?project=${project}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, ...payload }),
        },
      );
      const b = await r.json().catch(() => ({}));
      if (!r.ok || b.ok === false) {
        setActionNote(`${action} failed: ${b.detail ?? r.statusText}`);
      } else {
        setActionNote(`${action} ok.`);
        setEditing(false);
        if (action === "supersede" && b.entry?.id) {
          navigate(`/memory/${b.entry.id}`);
          return;
        }
      }
    } catch (e) {
      setActionNote(`${action} failed: ${(e as Error).message ?? e}`);
    } finally {
      setActionBusy(false);
    }
  };

  const type = (entry.type ?? "").toLowerCase();
  const status = (entry.status ?? "").toLowerCase();
  const classification = (entry.classification ?? "").toLowerCase();
  const typeTone: PillTone = domainTone("memoryType", type) ?? "slate";
  const statusTone: PillTone = domainTone("memoryStatus", status) ?? "slate";

  return (
    <Page>
      <BackLink onClick={() => navigate("/memory")} />

      <header className="space-y-2">
        <div className="flex items-start gap-3 flex-wrap">
          <h1 className="text-[20px] font-[650] leading-tight tracking-[-0.01em] flex-1 min-w-0 break-words">
            {entry.name ?? "—"}
          </h1>
          <div className="flex items-center gap-1 shrink-0">
            {type && <Badge tone={typeTone}>{type}</Badge>}
            {status && <Badge tone={statusTone}>{status}</Badge>}
            {classification && <Badge tone="slate">{classification}</Badge>}
          </div>
        </div>
        {entry.summary && (
          <p className="text-base text-[color:var(--text-secondary)] leading-relaxed max-w-3xl">
            {entry.summary}
          </p>
        )}
        {!entry.summary && (
          <p className="text-sm text-[color:var(--text-muted)] italic">
            Summarizing — the memory-summary worker hasn't filled this entry's
            one-line summary yet. It usually appears within a minute.
          </p>
        )}
      </header>

      <Card>
        <div className="flex items-center justify-between gap-3">
          <SectionLabel>Description</SectionLabel>
          <div className="flex items-center gap-1.5">
            {!editing && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => { setDraft(entry.description ?? ""); setEditing(true); }}
                className="text-2xs uppercase tracking-wider px-2 py-1 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40"
              >
                Edit
              </button>
            )}
            <button
              type="button"
              disabled={actionBusy || status === "retired"}
              onClick={() => doAction("retire")}
              className="text-2xs uppercase tracking-wider px-2 py-1 rounded disabled:opacity-40"
              style={{ background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)" }}
            >
              Retire
            </button>
          </div>
        </div>
        {actionNote && <div className="text-2xs opacity-70 mt-1">{actionNote}</div>}
        {editing ? (
          <div className="mt-2 space-y-2">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={8}
              className="w-full text-[13px] rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] p-3 leading-relaxed resize-y font-sans"
            />
            <div className="flex gap-2">
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => doAction("edit", { description: draft })}
                className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
                style={{ background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)" }}
              >
                Save edit
              </button>
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => doAction("supersede", { description: draft })}
                className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
                style={{ background: "var(--accent-violet-bg)", color: "var(--accent-violet-fg)" }}
                title="store a new generation under the same name (archives this one)"
              >
                Supersede
              </button>
              <button
                type="button"
                onClick={() => setEditing(false)}
                className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded bg-[color:var(--midground-base)]/15"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-1">
            {entry.description
              ? <EvidenceView text={entry.description} />
              : <Empty>(empty)</Empty>}
          </div>
        )}
      </Card>

      <Card>
        <SectionLabel>Meta</SectionLabel>
        <dl className="grid grid-cols-[160px_1fr] gap-y-2 text-sm">
          <Meta label="id"><span className="font-mono">{entry.id}</span></Meta>
          <Meta label="domain">{entry.domain || "—"}</Meta>
          <Meta label="memory_type">{entry.memory_type || "—"}</Meta>
          <Meta label="importance">
            {typeof entry.importance === "number" ? (
              <span className="inline-flex items-center gap-2">
                <span
                  className="inline-block w-2 h-2 rounded-full"
                  style={{ background: `var(--accent-${importanceTone(entry.importance)}-fg)` }}
                  aria-hidden
                />
                {entry.importance} / 10
              </span>
            ) : "—"}
          </Meta>
          <Meta label="generation">{entry.generation ?? 1}</Meta>
          <Meta label="effectiveness">
            {typeof entry.effectiveness === "number" ? (
              <span className={entry.effectiveness > 0
                ? "text-[color:var(--accent-emerald-fg)]"
                : entry.effectiveness < 0
                  ? "text-[color:var(--accent-rose-fg)]"
                  : "opacity-70"
              }>
                {entry.effectiveness.toFixed(2)}
              </span>
            ) : "—"}
          </Meta>
          <Meta label="recall count">{entry.recall_count ?? 0}</Meta>
          <Meta label="last recalled">{shortDate(entry.last_recalled)}</Meta>
          <Meta label="valid since">{shortDate(entry.valid_at)}</Meta>
          {entry.invalid_at && (
            <Meta label="invalidated">{shortDate(entry.invalid_at)}</Meta>
          )}
          <Meta label="recorded">{shortDate(entry.recorded_at)}</Meta>
        </dl>
      </Card>

      {entry.evidence && Object.keys(entry.evidence).length > 0 && (
        <Card>
          <SectionLabel>Evidence</SectionLabel>
          <EvidenceTable evidence={entry.evidence} />
        </Card>
      )}

      {chain.length > 1 && (
        <Card>
          <SectionLabel>Supersede chain ({chain.length} generation{chain.length === 1 ? "" : "s"})</SectionLabel>
          <ol className="space-y-2">
            {chain.map((c) => {
              const isMe = c.id === entry.id;
              const isLive = !c.invalid_at && c.status === "active";
              return (
                <li
                  key={c.id}
                  className={
                    "rounded-md border p-3 text-sm " +
                    (isMe
                      ? "border-[color:var(--border-strong)] bg-[color:var(--surface-2)]"
                      : "border-[color:var(--border-default)] bg-transparent opacity-80")
                  }
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono text-2xs opacity-70">gen {c.generation ?? 1}</span>
                    {isLive ? (
                      <Badge tone="emerald">active</Badge>
                    ) : (
                      <Badge tone="slate">archived</Badge>
                    )}
                    {isMe && <Badge tone="amber">viewing</Badge>}
                    <span className="ml-auto text-2xs opacity-50 font-mono">
                      {shortDate(c.valid_at)}
                    </span>
                  </div>
                  {!isMe && c.id && (
                    <button
                      onClick={() => navigate(`/memory/${c.id}`)}
                      className="mt-1 text-[12px] underline opacity-80 hover:opacity-100"
                    >
                      open gen {c.generation ?? 1}
                    </button>
                  )}
                </li>
              );
            })}
          </ol>
        </Card>
      )}
    </Page>
  );
}

function BackLink({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100"
    >
      ← back to memory
    </button>
  );
}

function Badge({ tone, children }: { tone: PillTone; children: React.ReactNode }) {
  return (
    <span
      className="text-2xs uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1"
      style={{
        background: `var(--accent-${tone}-bg)`,
        color: `var(--accent-${tone}-fg)`,
        boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)`,
      }}
    >
      {children}
    </span>
  );
}

// Evidence rendered as a key/value table (no raw JSON.stringify). Known keys
// — file_paths, commit, pr — render as links; everything else as readable text.
function EvidenceTable({ evidence }: { evidence: Record<string, unknown> }) {
  const REPO = "https://github.com/siegeon/.prism";
  const renderVal = (key: string, val: unknown): React.ReactNode => {
    if (Array.isArray(val)) {
      return (
        <ul className="space-y-0.5">
          {val.map((v, i) => <li key={i}>{renderVal(key, v)}</li>)}
        </ul>
      );
    }
    const s = String(val ?? "");
    if (key === "file_paths" || key === "file_path") {
      return <span className="font-mono text-[12px] text-[color:var(--accent-teal-fg)] break-all">{s}</span>;
    }
    if (key === "commit") {
      return (
        <a href={`${REPO}/commit/${s}`} target="_blank" rel="noreferrer"
           className="font-mono text-[12px] underline decoration-dotted text-[color:var(--accent-violet-fg)]">
          {s.slice(0, 12)}
        </a>
      );
    }
    if (key === "pr") {
      const num = s.replace(/[^0-9]/g, "");
      return (
        <a href={`${REPO}/pull/${num}`} target="_blank" rel="noreferrer"
           className="font-mono text-[12px] underline decoration-dotted text-[color:var(--accent-violet-fg)]">
          #{num || s}
        </a>
      );
    }
    return <span className="text-[13px] break-words">{s}</span>;
  };
  return (
    <dl className="grid grid-cols-[140px_1fr] gap-x-4 gap-y-2 mt-1 text-sm">
      {Object.entries(evidence).map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-[color:var(--text-label)] font-mono text-[12px]">{k}</dt>
          <dd className="min-w-0">{renderVal(k, v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-[color:var(--text-label)]">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}
