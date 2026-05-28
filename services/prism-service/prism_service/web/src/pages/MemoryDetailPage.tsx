import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import {
  Page, Card, SectionLabel, Empty, ErrorBanner, type PillTone,
} from "@/components/ui";

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

const TYPE_TONE: Record<string, PillTone> = {
  expertise: "teal",
  convention: "sage",
  decision: "amber",
  "anti-pattern": "rose",
  feedback: "violet",
  project: "amber",
  reference: "teal",
  user: "violet",
  pattern: "teal",
  failure: "rose",
};

const STATUS_TONE: Record<string, PillTone> = {
  active: "emerald",
  stale: "amber",
  retired: "slate",
  archived: "slate",
  needs_review: "amber",
};

function importanceTone(n: number): PillTone {
  if (n >= 9) return "rose";
  if (n >= 7) return "amber";
  if (n >= 4) return "sage";
  return "slate";
}

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

  const type = (entry.type ?? "").toLowerCase();
  const status = (entry.status ?? "").toLowerCase();
  const classification = (entry.classification ?? "").toLowerCase();
  const typeTone: PillTone = TYPE_TONE[type] ?? "slate";
  const statusTone: PillTone = STATUS_TONE[status] ?? "slate";

  return (
    <Page>
      <BackLink onClick={() => navigate("/memory")} />

      <header className="space-y-2">
        <div className="flex items-start gap-3 flex-wrap">
          <h1 className="font-serif text-3xl tracking-tight flex-1 min-w-0 break-words">
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
        <SectionLabel>Description</SectionLabel>
        <pre className="whitespace-pre-wrap text-[13px] leading-relaxed font-sans p-4 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)]">
          {entry.description || "(empty)"}
        </pre>
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
                ? "text-emerald-300/90"
                : entry.effectiveness < 0
                  ? "text-rose-300/90"
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
          <pre className="text-[12px] leading-relaxed font-mono p-3 rounded-md bg-[color:var(--surface-2)] border border-[color:var(--border-default)] overflow-x-auto">
            {JSON.stringify(entry.evidence, null, 2)}
          </pre>
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
                    <span className="font-mono text-[11px] opacity-70">gen {c.generation ?? 1}</span>
                    {isLive ? (
                      <Badge tone="emerald">active</Badge>
                    ) : (
                      <Badge tone="slate">archived</Badge>
                    )}
                    {isMe && <Badge tone="amber">viewing</Badge>}
                    <span className="ml-auto text-[11px] opacity-50 font-mono">
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
      className="text-[11px] uppercase tracking-wider opacity-60 hover:opacity-100"
    >
      ← back to memory
    </button>
  );
}

function Badge({ tone, children }: { tone: PillTone; children: React.ReactNode }) {
  return (
    <span
      className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1"
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

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-[color:var(--text-label)]">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}
