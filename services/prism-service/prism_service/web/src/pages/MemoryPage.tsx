import { useEffect, useState, useMemo } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Kpi, SectionLabel, Pill, Empty } from "@/components/ui";

type Entry = {
  name?: string;
  type?: string;
  classification?: string;
  status?: string;
  description?: string;
  domain?: string;
};

const TYPES = ["all", "expertise", "convention", "decision", "anti-pattern"];
const STATUSES = ["all", "active", "stale", "retired"];

export default function MemoryPage() {
  const [project] = useProject();
  const [domains, setDomains] = useState<string[]>([]);
  const [stats, setStats] = useState<Record<string, number>>({});
  const [domain, setDomain] = useState<string>("all");
  const [type, setType] = useState<string>("all");
  const [status, setStatus] = useState<string>("all");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [bump, setBump] = useState(0);

  useEffect(() => {
    api.get<{ domains: string[]; stats: Record<string, number> }>(`/api/memory/domains?project=${project}`)
      .then((d) => { setDomains(d.domains); setStats(d.stats); })
      .catch(() => { setDomains([]); setStats({}); });
  }, [project, bump]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(t);
  }, [notice]);

  const importClaudeMemories = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ imported: number; skipped: number; failed: number; memory_dir: string }>(
        `/api/memory/import-claude-memories?project=${project}`, {},
      );
      setNotice(
        r.imported > 0
          ? `Imported ${r.imported} memor${r.imported === 1 ? "y" : "ies"} from ${r.memory_dir} — skipped ${r.skipped}, failed ${r.failed}. Re-running is idempotent (supersedes, not duplicates).`
          : `No new memories imported from ${r.memory_dir} (${r.skipped} skipped, ${r.failed} failed). All already present, or directory empty.`
      );
      setBump((b) => b + 1);
    } catch (e) {
      setNotice(`Import failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const params = new URLSearchParams({ project });
    if (domain !== "all") params.set("domain", domain);
    if (type !== "all") params.set("type", type);
    if (status !== "all") params.set("status", status);
    api.get<{ entries: Entry[] }>(`/api/memory/entries?${params}`)
      .then((d) => setEntries(d.entries)).catch(() => setEntries([]));
  }, [project, domain, type, status, bump]);

  const total = useMemo(() => Object.values(stats).reduce((a, b) => a + b, 0), [stats]);

  return (
    <Page>
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="font-serif text-3xl tracking-tight">Memory</h1>
          <p className="text-sm opacity-60 mt-1">
            Persisted patterns, conventions, decisions, and failures —
            queried by every Claude session via <code className="opacity-80">memory_recall</code>.
            Backfill from Claude Code's auto-memory if this page is empty.
          </p>
        </div>
        <button
          onClick={importClaudeMemories}
          disabled={busy}
          className="px-4 py-2 rounded-md bg-[color:var(--midground-base)] text-[color:var(--background-base)] text-xs uppercase tracking-wider disabled:opacity-40"
        >
          {busy ? "Working…" : "Import Claude memories"}
        </button>
      </div>

      {notice && (
        <div className="fixed bottom-6 right-6 z-40 max-w-[420px] rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/95 backdrop-blur-sm shadow-lg px-4 py-3 text-[12px] flex items-start gap-3">
          <span className="flex-1 opacity-90 leading-relaxed">{notice}</span>
          <button
            onClick={() => setNotice(null)}
            className="text-[10px] uppercase tracking-wider opacity-60 hover:opacity-100 shrink-0"
          >
            dismiss
          </button>
        </div>
      )}

      <section className="flex flex-wrap gap-3">
        <Kpi label="Entries" value={total} />
        <Kpi label="Domains" value={domains.length} />
        <Kpi label="Showing" value={entries.length} />
      </section>

      <Card>
        <SectionLabel>Domain</SectionLabel>
        <div className="flex flex-wrap gap-2 mb-4">
          <Pill active={domain === "all"} onClick={() => setDomain("all")}>all</Pill>
          {domains.map((d) => (
            <Pill key={d} active={domain === d} onClick={() => setDomain(d)}>{d}</Pill>
          ))}
        </div>
        <SectionLabel>Type</SectionLabel>
        <div className="flex flex-wrap gap-2 mb-4">
          {TYPES.map((t) => <Pill key={t} active={type === t} onClick={() => setType(t)}>{t}</Pill>)}
        </div>
        <SectionLabel>Status</SectionLabel>
        <div className="flex flex-wrap gap-2">
          {STATUSES.map((s) => <Pill key={s} active={status === s} onClick={() => setStatus(s)}>{s}</Pill>)}
        </div>
      </Card>

      <Card>
        <SectionLabel>{entries.length} entr{entries.length === 1 ? "y" : "ies"}</SectionLabel>
        {entries.length === 0 ? (
          <Empty>No entries match these filters.</Empty>
        ) : (
          <div className="divide-y divide-[color:var(--midground-base)]/10">
            {entries.map((e, i) => (
              <div key={i} className="py-3">
                <div className="flex items-center gap-3 text-sm">
                  <span className="font-medium flex-1 truncate">{e.name ?? "—"}</span>
                  {e.type && <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-[color:var(--midground-base)]/10 opacity-70">{e.type}</span>}
                  {e.classification && <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-[color:var(--midground-base)]/10 opacity-70">{e.classification}</span>}
                  {e.status && <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-[color:var(--midground-base)]/10 opacity-70">{e.status}</span>}
                </div>
                {e.description && <div className="text-xs opacity-60 mt-1 line-clamp-2">{e.description}</div>}
              </div>
            ))}
          </div>
        )}
      </Card>
    </Page>
  );
}
