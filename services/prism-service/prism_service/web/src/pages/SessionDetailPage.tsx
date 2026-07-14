import { useEffect, useState, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Kpi, SectionLabel, Empty } from "@/components/ui";
import XrefLink from "@/components/XrefLink";
import { fmtTokens } from "@/lib/format";

// One scored session, expanded. Mirrors conductor_service.get_session_outcomes
// scalar columns (snake_case_with_units) PLUS the two path lists the
// traceability slice stops collapsing to counts — so a file a session touched
// clicks through to /artifact via XrefLink instead of dead-ending as a number.
type SessionDetail = {
  session_id?: string;
  timestamp?: string;
  recorded_at?: string;
  tokens_used?: number;
  tokens?: number;
  duration_s?: number;
  duration?: number;
  files_read?: number;
  files_modified?: number;
  skills_invoked?: number;
  tokens_per_file?: number | null;
  files_read_paths?: string[];
  files_modified_paths?: string[];
};

const fmtTs = (s?: string) =>
  s ? s.replace("T", " ").replace(/\.\d+/, "").slice(0, 19) : "";

// A file-path list rendered as clickable xref chips, with a graceful state for
// legacy sessions whose paths were never recorded (only counts survive).
function PathList({ label, paths }: { label: string; paths: string[] }) {
  return (
    <Card>
      <SectionLabel>{label} ({paths.length})</SectionLabel>
      {paths.length === 0 ? (
        <Empty>No file data recorded for this session.</Empty>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {paths.map((p, i) => (
            <XrefLink key={`${p}-${i}`} token={p} />
          ))}
        </div>
      )}
    </Card>
  );
}

export default function SessionDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [project] = useProject();
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api.get<{ session: SessionDetail | null }>(`/api/sessions/${id}?project=${project}`)
      .then((d) => { setSession(d.session); setError(null); })
      .catch((e) => { setSession(null); setError((e as Error).message ?? String(e)); });
  }, [id, project]);

  useEffect(() => { load(); }, [load]);

  const back = (
    <Link
      to="/sessions"
      className="text-[11px] uppercase tracking-wider opacity-60 hover:opacity-100"
    >
      ← Sessions
    </Link>
  );

  if (error) {
    return (
      <Page>
        {back}
        <Card><Empty>{error}</Empty></Card>
      </Page>
    );
  }

  if (!session) {
    return (
      <Page>
        {back}
        <Card><Empty>Loading…</Empty></Card>
      </Page>
    );
  }

  const tk = session.tokens_used ?? session.tokens ?? 0;
  const dur = session.duration_s ?? session.duration ?? 0;
  const readPaths = session.files_read_paths ?? [];
  const modPaths = session.files_modified_paths ?? [];

  return (
    <Page>
      {back}
      <div>
        <div className="font-mono text-lg break-all text-[color:var(--text-primary)]">
          {session.session_id ?? id}
        </div>
        <div className="text-[11px] opacity-50 mt-1">
          {fmtTs(session.timestamp ?? session.recorded_at) || "no timestamp"}
        </div>
      </div>

      <section className="flex flex-wrap gap-3">
        <Kpi label="Tokens" value={fmtTokens(tk)} />
        <Kpi label="Duration" value={`${Math.round(dur)}s`} />
        {/* path lists are ground truth when present; legacy scalar counts can
            lag them (rows imported pre-path-persistence, backfilled later) */}
        <Kpi label="Files read" value={Math.max(session.files_read ?? 0, readPaths.length)} />
        <Kpi label="Files modified" value={Math.max(session.files_modified ?? 0, modPaths.length)} />
        <Kpi label="Skills" value={session.skills_invoked ?? 0} />
        <Kpi
          label="Tokens / file"
          value={session.tokens_per_file ? `${fmtTokens(session.tokens_per_file)}` : "—"}
        />
      </section>

      <PathList label="Files read" paths={readPaths} />
      <PathList label="Files modified" paths={modPaths} />
    </Page>
  );
}
