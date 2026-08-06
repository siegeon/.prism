import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty } from "@/components/ui";
import { Lozenge } from "@/components/Lozenge";
import { stepLabel } from "@/lib/workflowChips";

// Inbox is a READ PROJECTION of conductor state (task 0784729f, approved
// prototype). It never creates, advances, or mutates anything — every card
// here deep-links into the task page that already owns the gate, the
// evidence, and Approve. Deliberately not called "My Work": Work (TasksPage,
// its own My Work / Team toggle) is the full list you go and query; Inbox is
// only what needs you, and it empties.

type NeedsYouItem = {
  task_id: string;
  title: string;
  workflow_step: string;
  gate_reason: string;
  parent_id: string;
  url: string;
};

type ActivityItem = {
  task_id: string;
  title: string;
  workflow_step: string;
  step_position: string;
  parent_id: string;
  url: string;
};

type InboxState = {
  needs_you: NeedsYouItem[];
  activity: ActivityItem[];
};

export default function InboxPage() {
  const [project] = useProject();
  const [data, setData] = useState<InboxState | null>(null);

  const load = useCallback(() => {
    api.get<InboxState>(`/api/inbox?project=${encodeURIComponent(project)}`)
      .then(setData)
      .catch(() => setData(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  const needsYou = data?.needs_you ?? [];
  const activity = data?.activity ?? [];

  return (
    <Page>
      <Card>
        <SectionLabel>Needs you</SectionLabel>
        <p className="text-2xs opacity-60 mt-1 mb-3">
          What needs you, and what happened for you. An agent finished the work but
          cannot sign its own gate — a distinct reviewer has to.
        </p>
        {needsYou.length === 0 ? (
          <Empty>Nothing needs you right now.</Empty>
        ) : (
          <div className="space-y-2">
            {needsYou.map((item) => (
              <Link
                key={item.task_id}
                to={item.url}
                className="block rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] px-4 py-3 hover:bg-[color:var(--surface-2)] transition-colors"
              >
                <div className="flex items-center gap-2">
                  <Lozenge tone="warn">{stepLabel(item.workflow_step) || "gate"}</Lozenge>
                  <span className="text-sm font-medium text-[color:var(--text-primary)]">{item.title}</span>
                </div>
                {item.gate_reason && (
                  <div className="text-2xs text-[color:var(--text-muted)] mt-1">{item.gate_reason}</div>
                )}
              </Link>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <SectionLabel>Happening for you</SectionLabel>
        {activity.length === 0 ? (
          <Empty>Nothing in flight right now.</Empty>
        ) : (
          <div className="space-y-2">
            {activity.map((item) => (
              <Link
                key={item.task_id}
                to={item.url}
                className="flex items-center gap-2 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] px-4 py-3 hover:bg-[color:var(--surface-2)] transition-colors"
              >
                <Lozenge tone="info">{stepLabel(item.workflow_step)}</Lozenge>
                <span className="text-sm text-[color:var(--text-primary)] flex-1 min-w-0 truncate">{item.title}</span>
                {item.step_position && (
                  <span className="text-2xs text-[color:var(--text-muted)] shrink-0">{item.step_position}</span>
                )}
              </Link>
            ))}
          </div>
        )}
      </Card>
    </Page>
  );
}
