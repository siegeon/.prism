import { useState, useEffect, useCallback } from "react";
import { Search, RefreshCw, Sparkles, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

type BrainStatus = {
  doc_count?: number;
  entity_count?: number;
  vector_enabled?: boolean;
  last_reindex?: string | null;
};

type SearchResult = {
  entity_name?: string;
  entity_kind?: string;
  domain?: string;
  file?: string;
  source?: string;
  rrf_score?: number;
  score?: number;
};

type AskSource = {
  index: number;
  file: string;
  entity_name: string;
  entity_kind: string;
  score: number;
};

type AskResponse = {
  question: string;
  project: string;
  answer: string;
  sources: AskSource[];
  run_id: string;
  exit_code: number;
  duration_s: number;
  tokens: { input: number; output: number };
};

type Mode = "search" | "ask";

const DOMAINS = ["all", "py", "ts", "js", "md", "expertise"];

function Kpi({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex-1 min-w-[150px] rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--background-base)]/40 p-4">
      <div className="text-[10px] uppercase tracking-wider opacity-60 mb-2">{label}</div>
      <div className="text-2xl font-semibold leading-none">{value}</div>
    </div>
  );
}

export default function BrainPage() {
  const [project] = useState("default");
  const [status, setStatus] = useState<BrainStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("search");
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState<string>("all");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [askResponse, setAskResponse] = useState<AskResponse | null>(null);
  const [askError, setAskError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [asking, setAsking] = useState(false);
  const [reindexing, setReindexing] = useState(false);

  const loadStatus = useCallback(() => {
    api.get<BrainStatus>(`/api/brain/status?project=${project}`)
      .then(s => { setStatus(s); setStatusError(null); })
      .catch(e => setStatusError(String(e)));
  }, [project]);

  useEffect(loadStatus, [loadStatus]);

  const doSearch = useCallback(async () => {
    if (!query.trim()) return;
    setSearching(true);
    setAskResponse(null);
    setAskError(null);
    try {
      const params = new URLSearchParams({ q: query.trim(), project, limit: "20" });
      if (domain !== "all") params.set("domain", domain);
      const data = await api.get<{ results: SearchResult[] }>(`/api/brain/search?${params}`);
      setResults(data.results);
    } catch { setResults([]); } finally { setSearching(false); }
  }, [query, domain, project]);

  const doAsk = useCallback(async () => {
    if (!query.trim()) return;
    setAsking(true);
    setAskError(null);
    setResults(null);
    try {
      const data = await api.post<AskResponse>("/api/brain/ask", {
        q: query.trim(),
        project,
        domain: domain === "all" ? null : domain,
      });
      setAskResponse(data);
    } catch (e) {
      setAskError(String((e as Error).message ?? e));
    } finally {
      setAsking(false);
    }
  }, [query, domain, project]);

  const submit = useCallback(() => {
    if (mode === "ask") doAsk();
    else doSearch();
  }, [mode, doAsk, doSearch]);

  const doReindex = useCallback(async () => {
    setReindexing(true);
    try { await api.post(`/api/brain/reindex?project=${project}`, {}); loadStatus(); }
    catch { /* surface via status error */ } finally { setReindexing(false); }
  }, [project, loadStatus]);

  return (
    <div className="p-8 space-y-6 w-full min-w-[720px]">
      <section className="flex flex-wrap gap-3">
        <Kpi label="Documents" value={status?.doc_count ?? "—"} />
        <Kpi label="Entities" value={status?.entity_count ?? "—"} />
        <Kpi label="Vectors" value={
          status?.vector_enabled === undefined ? "—"
            : <span className={cn("text-base px-2 py-1 rounded-md",
                status.vector_enabled
                  ? "bg-emerald-500/15 text-emerald-300"
                  : "bg-rose-500/15 text-rose-300")}>
                {status.vector_enabled ? "Enabled" : "Disabled"}
              </span>} />
        <Kpi label="Last Reindex" value={
          <span className="font-mono text-sm">{String(status?.last_reindex ?? "—").slice(0,19)}</span>} />
        <button
          onClick={doReindex}
          disabled={reindexing}
          className="self-stretch min-w-[150px] rounded-md border border-[color:var(--midground-base)]/30 bg-[color:var(--midground-base)]/5 hover:bg-[color:var(--midground-base)]/10 px-4 transition-colors flex items-center justify-center gap-2 text-sm uppercase tracking-wider disabled:opacity-50"
        >
          <RefreshCw className={cn("w-4 h-4", reindexing && "animate-spin")} />
          {reindexing ? "Reindexing…" : "Reindex"}
        </button>
      </section>
      {statusError && (
        <div className="rounded-md border border-rose-500/30 bg-rose-500/10 text-rose-200 px-4 py-3 text-sm">
          Brain status unreachable — is the PRISM service running on :7777? <span className="opacity-60">({statusError.slice(0, 120)})</span>
        </div>
      )}

      <section className="rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--background-base)]/40 p-5">
        <div className="flex items-center justify-between mb-3">
          <div className="text-[10px] uppercase tracking-wider opacity-60">
            {mode === "ask" ? "Ask Claude (grounded in the knowledge base)" : "Search the knowledge base"}
          </div>
          <div className="inline-flex rounded-md border border-[color:var(--midground-base)]/20 overflow-hidden text-[10px] uppercase tracking-wider">
            <button
              type="button"
              onClick={() => setMode("search")}
              className={cn(
                "px-3 py-1 transition-colors",
                mode === "search"
                  ? "bg-[color:var(--midground-base)] text-[color:var(--background-base)]"
                  : "opacity-60 hover:opacity-100",
              )}
            >
              <Search className="w-3 h-3 inline -mt-0.5 mr-1" />
              Search
            </button>
            <button
              type="button"
              onClick={() => setMode("ask")}
              className={cn(
                "px-3 py-1 transition-colors",
                mode === "ask"
                  ? "bg-[color:var(--midground-base)] text-[color:var(--background-base)]"
                  : "opacity-60 hover:opacity-100",
              )}
            >
              <Sparkles className="w-3 h-3 inline -mt-0.5 mr-1" />
              Ask Claude
            </button>
          </div>
        </div>
        <div className="flex gap-3">
          <div className="flex-1 flex items-center gap-2 rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/50 px-3 py-2">
            {mode === "ask"
              ? <Sparkles className="w-4 h-4 opacity-60" />
              : <Search className="w-4 h-4 opacity-60" />}
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              placeholder={mode === "ask"
                ? "Ask a question about this codebase…"
                : "Search concepts, entities, files…"}
              className="flex-1 bg-transparent outline-none text-sm placeholder:opacity-40"
            />
          </div>
          <button
            onClick={submit}
            disabled={searching || asking || !query.trim()}
            className="px-5 rounded-md border border-[color:var(--midground-base)]/30 bg-[color:var(--midground-base)]/10 hover:bg-[color:var(--midground-base)]/20 text-sm uppercase tracking-wider disabled:opacity-40 transition-colors min-w-[120px]"
          >
            {mode === "ask"
              ? (asking ? <span className="inline-flex items-center gap-2"><Loader2 className="w-3 h-3 animate-spin" />Asking…</span> : "Ask")
              : (searching ? "Searching…" : "Search")}
          </button>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          {DOMAINS.map((d) => (
            <button key={d} onClick={() => setDomain(d)}
              className={cn(
                "px-3 py-1 rounded-full text-[11px] uppercase tracking-wider transition-colors",
                domain === d
                  ? "bg-[color:var(--midground-base)] text-[color:var(--background-base)]"
                  : "bg-[color:var(--midground-base)]/10 text-[color:var(--midground-base)]/70 hover:bg-[color:var(--midground-base)]/20"
              )}>{d}</button>
          ))}
        </div>
        {mode === "ask" && (
          <p className="mt-3 text-[11px] opacity-60 leading-snug">
            Pulls top hits from the Brain (BM25 + vector + graph), feeds them
            to <code className="font-mono">claude -p</code> as grounded context.
            Costs your Claude subscription one invocation per question
            (~5-30s depending on complexity).
          </p>
        )}
      </section>

      {askError && (
        <div className="rounded-md border border-rose-500/30 bg-rose-500/10 text-rose-200 px-4 py-3 text-sm">
          Ask failed: {askError}
        </div>
      )}

      {askResponse && (
        <section className="rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--background-base)]/40 p-5 space-y-4">
          <div className="flex items-center justify-between text-[10px] uppercase tracking-wider opacity-60">
            <span>
              Answer
              {askResponse.duration_s > 0 && (
                <span className="ml-2 opacity-70">· {askResponse.duration_s.toFixed(1)}s</span>
              )}
              {askResponse.tokens.output > 0 && (
                <span className="ml-2 opacity-70">· {askResponse.tokens.output} output tokens</span>
              )}
            </span>
            {askResponse.run_id && (
              <span className="font-mono opacity-50">run {askResponse.run_id.slice(0, 8)}</span>
            )}
          </div>
          <div className="prose prose-invert max-w-none text-sm whitespace-pre-wrap font-sans leading-relaxed">
            {askResponse.answer}
          </div>
          {askResponse.sources.length > 0 && (
            <div className="border-t border-[color:var(--midground-base)]/10 pt-3">
              <div className="text-[10px] uppercase tracking-wider opacity-60 mb-2">
                Cited sources ({askResponse.sources.length})
              </div>
              <ul className="space-y-1">
                {askResponse.sources.map((s) => (
                  <li key={s.index} className="flex items-baseline gap-3 text-xs font-mono opacity-80">
                    <span className="opacity-50 w-6">[{s.index}]</span>
                    <span className="flex-1 truncate">{s.file}</span>
                    {s.entity_name && <span className="opacity-60">{s.entity_name}</span>}
                    {s.score > 0 && <span className="opacity-50">{s.score.toFixed(3)}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {results && (
        <section>
          <div className="text-[10px] uppercase tracking-wider opacity-60 mb-3">
            {results.length} result{results.length === 1 ? "" : "s"}
          </div>
          <div className="rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--background-base)]/40 divide-y divide-[color:var(--midground-base)]/10">
            {results.length === 0 && (
              <div className="px-5 py-8 text-center text-sm opacity-60">No matches.</div>
            )}
            {results.map((r, i) => {
              const score = r.rrf_score ?? r.score ?? 0;
              return (
                <div key={i} className="px-5 py-3 flex items-start gap-4 hover:bg-[color:var(--midground-base)]/5 transition-colors">
                  <div className="w-8 text-xs opacity-50 font-mono pt-0.5">{i + 1}</div>
                  <div className="w-16 text-xs font-mono opacity-70 pt-0.5">{score ? score.toFixed(3) : "—"}</div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">{r.entity_name ?? "—"}</div>
                    <div className="text-xs opacity-60 truncate font-mono mt-0.5">{r.file ?? r.source ?? ""}</div>
                  </div>
                  {r.entity_kind && (
                    <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-[color:var(--midground-base)]/10 opacity-70">{r.entity_kind}</span>
                  )}
                  {r.domain && (
                    <span className="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded bg-[color:var(--midground-base)]/10 opacity-70">{r.domain}</span>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}
