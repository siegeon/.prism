import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, Button } from "@/components/ui";
import { Lozenge } from "@/components/Lozenge";

// The Queue is where SIGNALS arrive over their channel (owner's model,
// mx-0889e4). A signal is NOT a task -- it becomes one only when the owner
// TYPES what to do and clicks "Make it a task" (task 01d05bff). This is not
// a view of tasks: Work (/tasks) already is that.

type Signal = {
  id: string;
  channel: string;
  channel_ref: string;
  subject: string;
  body: string;
  sender: string;
  arrived_at: string;
  state: string;
  task_id: string;
  matches?: Record<string, unknown>;
};

function relTime(iso?: string): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 604800) return `${Math.floor(s / 86400)}d`;
  return `${Math.floor(s / 604800)}w`;
}

function groupByChannel(signals: Signal[]): [string, Signal[]][] {
  const groups = new Map<string, Signal[]>();
  for (const s of signals) {
    const key = s.channel || "ui";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(s);
  }
  return [...groups.entries()];
}

export default function QueuePage() {
  const [project] = useProject();
  const [open, setOpen] = useState<Signal[]>([]);
  const [became, setBecame] = useState<Signal[]>([]);

  const load = useCallback(() => {
    api
      .get<{ signals: Signal[] }>(
        `/api/signals?project=${encodeURIComponent(project)}&state=open`,
      )
      .then((d) => setOpen(d.signals))
      .catch(() => setOpen([]));
    api
      .get<{ signals: Signal[] }>(
        `/api/signals?project=${encodeURIComponent(project)}&state=became_task&limit=20`,
      )
      .then((d) => setBecame(d.signals))
      .catch(() => setBecame([]));
  }, [project]);

  // Only poll a tab someone is looking at (Sidebar useStaleness / TasksPage
  // precedent) — refetch on focus so the Queue is current the moment it's
  // seen.
  useEffect(() => {
    const tick = () => { if (!document.hidden) load(); };
    load();
    const t = setInterval(tick, 10000);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", tick); };
  }, [load]);

  const promote = useCallback((signalId: string, title: string, workflow: string) => {
    api
      .post(`/api/signals/${signalId}/promote?project=${encodeURIComponent(project)}`, { title, workflow })
      .then(load)
      .catch(() => {});
  }, [project, load]);

  const drop = useCallback((signalId: string, reason: string) => {
    api
      .post(`/api/signals/${signalId}/drop?project=${encodeURIComponent(project)}`, { reason })
      .then(load)
      .catch(() => {});
  }, [project, load]);

  const groups = groupByChannel(open);

  return (
    <Page>
      <div>
        <div className="text-lg font-semibold text-[color:var(--text-primary)]">Queue</div>
        <div className="text-[13px] text-[color:var(--text-secondary)] mt-1 max-w-[760px]">
          Signals arrive here over their channel. Nothing becomes a task
          until you type what should happen and click.
        </div>
      </div>

      <Card raised>
        <SectionLabel>Open</SectionLabel>
        {open.length === 0 ? (
          <Empty>Nothing in the queue — signals arrive here over their channels.</Empty>
        ) : (
          <div className="space-y-4">
            {groups.map(([channel, signals]) => (
              <div key={channel}>
                <Lozenge tone="neutral" className="mb-2">{channel}</Lozenge>
                <div className="space-y-2">
                  {signals.map((s) => (
                    <SignalRow key={s.id} signal={s} onPromote={promote} onDrop={drop} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {became.length > 0 && (
        <Card>
          <SectionLabel>Recently became tasks</SectionLabel>
          <div className="space-y-1.5">
            {became.map((s) => (
              <div key={s.id} className="flex items-center gap-2 text-xs">
                <span className="truncate" style={{ color: "var(--text-secondary)" }}>{s.subject}</span>
                <Link
                  to={`/tasks/${s.task_id}`}
                  className="shrink-0 hover:underline decoration-dotted underline-offset-2"
                  style={{ color: "var(--accent-teal-fg)" }}
                >
                  became task {s.task_id.slice(0, 8)}
                </Link>
              </div>
            ))}
          </div>
        </Card>
      )}
    </Page>
  );
}

function SignalRow({ signal, onPromote, onDrop }: {
  signal: Signal;
  onPromote: (signalId: string, title: string, workflow: string) => void;
  onDrop: (signalId: string, reason: string) => void;
}) {
  const [title, setTitle] = useState(signal.subject);
  const [workflow, setWorkflow] = useState("triage");
  const [dropping, setDropping] = useState(false);
  const [reason, setReason] = useState("");
  const matches = Object.entries(signal.matches ?? {});

  return (
    <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] p-3">
      <div className="flex items-center gap-2">
        <span className="font-medium text-sm" style={{ color: "var(--text-primary)" }}>{signal.subject}</span>
        <span className="text-2xs shrink-0" style={{ color: "var(--text-muted)" }}>
          {signal.sender} · {relTime(signal.arrived_at)}
        </span>
      </div>
      <div className="text-xs mt-1 line-clamp-2" style={{ color: "var(--text-secondary)" }}>
        {signal.body}
      </div>
      {matches.length > 0 && (
        <div className="text-2xs mt-1 flex flex-wrap gap-x-3" style={{ color: "var(--text-muted)" }}>
          {matches.map(([k, v]) => <span key={k}>{k}: {String(v)}</span>)}
        </div>
      )}
      <div className="flex items-center gap-2 mt-2">
        <input
          data-queue-title-input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="what should happen?"
          aria-label="What should happen?"
          className="flex-1 px-2 py-1.5 text-xs rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)]"
          style={{ color: "var(--text-secondary)" }}
        />
        <select
          data-queue-workflow-select
          value={workflow}
          onChange={(e) => setWorkflow(e.target.value)}
          aria-label="Workflow"
          className="px-2 py-1.5 text-xs rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)]"
          style={{ color: "var(--text-secondary)" }}
        >
          <option value="triage">triage</option>
          <option value="implement">implement</option>
        </select>
        <Button
          type="button"
          variant="primary"
          onClick={() => onPromote(signal.id, title, workflow)}
        >
          Make it a task
        </Button>
        <Button type="button" onClick={() => setDropping((d) => !d)}>
          Drop
        </Button>
      </div>
      {dropping && (
        <div className="flex items-center gap-2 mt-2">
          <input
            data-queue-drop-reason
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="why drop this?"
            aria-label="Why drop this?"
            className="flex-1 px-2 py-1.5 text-xs rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)]"
            style={{ color: "var(--text-secondary)" }}
          />
          <Button type="button" onClick={() => onDrop(signal.id, reason)}>
            Confirm drop
          </Button>
        </div>
      )}
    </div>
  );
}
