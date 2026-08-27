import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, decideSignal, type SignalDecideAction } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, Button } from "@/components/ui";
import { Lozenge } from "@/components/Lozenge";
import LinkedText from "@/components/LinkedText";

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
  // Aligned text (task ed034701): the STE pipeline's output for
  // subject/body, empty until SignalStore.create() runs. subject/body
  // stay exactly as the signal arrived.
  aligned_subject?: string;
  aligned_body?: string;
  style?: Record<string, unknown>;
};

const FOCUS_PREVIEW_LIMIT = 6;

/** A long focus list shows a count and the first names, never every URN. */
function focusPreview(k: string, v: unknown): string {
  if (k === "focus" && Array.isArray(v) && v.length > FOCUS_PREVIEW_LIMIT) {
    const names = v.slice(0, FOCUS_PREVIEW_LIMIT).map((x) => String(x).split("/").pop()).join(", ");
    return `${v.length} nodes: ${names}, …`;
  }
  return String(v);
}

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

  // A firing rule becomes a decision on the Queue (task b1971944): an
  // "ontology" signal gets one of four answers instead of promote/drop.
  const decide = useCallback((signalId: string, action: SignalDecideAction, reason: string, focus: string[] = []) => {
    decideSignal(project, signalId, action, reason, focus)
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
                    <SignalRow key={s.id} signal={s} onPromote={promote} onDrop={drop} onDecide={decide} />
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

function SignalRow({ signal, onPromote, onDrop, onDecide }: {
  signal: Signal;
  onPromote: (signalId: string, title: string, workflow: string) => void;
  onDrop: (signalId: string, reason: string) => void;
  onDecide: (signalId: string, action: SignalDecideAction, reason: string, focus?: string[]) => void;
}) {
  const displaySubject = signal.aligned_subject || signal.subject;
  const displayBody = signal.aligned_body || signal.body;
  const [title, setTitle] = useState(displaySubject);
  const [workflow, setWorkflow] = useState("triage");
  const [dropping, setDropping] = useState(false);
  const [reason, setReason] = useState("");
  const matches = Object.entries(signal.matches ?? {});
  // A firing rule becomes a decision on the Queue (task b1971944): an
  // "ontology" signal gets Accept/Exempt/Fix/Codify instead of the plain
  // promote/drop row every other channel uses.
  const isRuleDecision = signal.channel === "ontology";

  return (
    <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] p-3">
      <div className="flex items-center gap-2">
        <span className="font-medium text-sm" style={{ color: "var(--text-primary)" }}>
          <LinkedText text={displaySubject} />
        </span>
        <span className="text-2xs shrink-0" style={{ color: "var(--text-muted)" }}>
          {signal.sender} · {relTime(signal.arrived_at)}
        </span>
      </div>
      <div className="text-xs mt-1 line-clamp-2" style={{ color: "var(--text-secondary)" }}>
        <LinkedText text={displayBody} />
      </div>
      <details className="text-2xs mt-1" style={{ color: "var(--text-muted)" }}>
        <summary className="cursor-pointer select-none">As arrived</summary>
        <div className="mt-1 max-h-40 overflow-y-auto break-all">
          <div>{signal.subject}</div>
          <div>{signal.body}</div>
        </div>
      </details>
      {matches.length > 0 && (
        <div className="text-2xs mt-1 flex flex-wrap gap-x-3" style={{ color: "var(--text-muted)" }}>
          {matches.map(([k, v]) => <span key={k}>{k}: {focusPreview(k, v)}</span>)}
        </div>
      )}
      {!isRuleDecision && (
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
      )}
      {!isRuleDecision && dropping && (
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
      {isRuleDecision && <RuleDecisionRow signal={signal} onDecide={onDecide} />}
    </div>
  );
}

// A firing rule becomes a decision on the Queue (task b1971944): the four
// answers an "ontology" signal carries instead of promote/drop. `focus`
// (task b1971944's exempt payload) comes off signal.matches.focus --
// the same focus-IRI list rule_decisions.on_rules_validated stashes there
// at post time, so exempting never needs a second round trip to fetch it.
function RuleDecisionRow({ signal, onDecide }: {
  signal: Signal;
  onDecide: (signalId: string, action: SignalDecideAction, reason: string, focus?: string[]) => void;
}) {
  const [reason, setReason] = useState("");
  const focusIris = Array.isArray((signal.matches as { focus?: unknown } | undefined)?.focus)
    ? ((signal.matches as { focus: string[] }).focus)
    : [];
  const [selected, setSelected] = useState<string[]>([]);
  const [showAllFocus, setShowAllFocus] = useState(false);
  const visibleFocus = showAllFocus ? focusIris : focusIris.slice(0, FOCUS_PREVIEW_LIMIT);
  const toggle = (iri: string) => {
    setSelected((cur) => (cur.includes(iri) ? cur.filter((x) => x !== iri) : [...cur, iri]));
  };

  return (
    <div className="mt-2 space-y-2" data-signal-decision>
      <textarea
        data-signal-decide-reason
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="why?"
        aria-label="Why?"
        rows={2}
        className="w-full px-2 py-1.5 text-xs rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)]"
        style={{ color: "var(--text-secondary)" }}
      />
      {focusIris.length > FOCUS_PREVIEW_LIMIT && (
        <div className="text-2xs" style={{ color: "var(--text-muted)" }} data-signal-decide-focus-count>
          {focusIris.length} nodes
        </div>
      )}
      {focusIris.length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-2xs" style={{ color: "var(--text-muted)" }}>
          {visibleFocus.map((iri) => (
            <label key={iri} className="flex items-center gap-1">
              <input
                data-signal-decide-focus
                type="checkbox"
                checked={selected.includes(iri)}
                onChange={() => toggle(iri)}
              />
              {iri.split("/").pop()}
            </label>
          ))}
          {focusIris.length > FOCUS_PREVIEW_LIMIT && !showAllFocus && (
            <button
              type="button"
              data-signal-decide-focus-show-all
              className="underline"
              onClick={() => setShowAllFocus(true)}>
              show all {focusIris.length}
            </button>
          )}
        </div>
      )}
      <div className="flex items-center gap-2">
        <Button type="button" data-signal-decide-accept variant="primary"
          onClick={() => onDecide(signal.id, "accept", reason)}>
          Accept
        </Button>
        <Button type="button" data-signal-decide-exempt
          onClick={() => onDecide(signal.id, "exempt", reason, selected)}>
          Exempt
        </Button>
        <Button type="button" data-signal-decide-fix
          onClick={() => onDecide(signal.id, "fix", reason)}>
          Fix
        </Button>
        <Button type="button" data-signal-decide-codify
          onClick={() => onDecide(signal.id, "codify", reason)}>
          Codify
        </Button>
      </div>
    </div>
  );
}
