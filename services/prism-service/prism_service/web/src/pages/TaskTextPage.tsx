// Dedicated screen for a single long task artifact (gate validation evidence,
// completion proof, description). Reached by clicking the summary row on the
// task detail — each artifact gets its OWN screen + URL, not an inline collapse.
import { useEffect, useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Empty } from "@/components/ui";

type Task = {
  id?: string;
  title?: string;
  gate_reason?: string;
  completion_proof?: string;
  description?: string;
  workflow_step?: string;
  gate_state?: string;
};

const SECTIONS: Record<string, { label: string; field: keyof Task }> = {
  validation: { label: "Gate validation", field: "gate_reason" },
  proof: { label: "Completion proof", field: "completion_proof" },
  description: { label: "Description", field: "description" },
};

export default function TaskTextPage() {
  const { id = "", section = "" } = useParams<{ id: string; section: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [project] = useProject();
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const back = (location.state as { from?: string } | null)?.from || `/tasks/${id}`;
  const meta = SECTIONS[section];

  useEffect(() => {
    if (!id) return;
    api
      .get<{ task: Task }>(`/api/tasks/${id}?project=${project}`)
      .then((d) => setTask(d.task))
      .catch((e) => setError((e as Error).message ?? "task not found"));
  }, [id, project]);

  const text = task && meta ? (task[meta.field] as string | undefined) : undefined;

  return (
    <Page>
      <button
        onClick={() => navigate(back)}
        className="text-[11px] uppercase tracking-wider opacity-60 hover:opacity-100 self-start"
      >
        ← back to task
      </button>
      <div>
        <h1 className="font-serif text-2xl tracking-tight">{meta?.label ?? "Detail"}</h1>
        {task?.title && <div className="text-[12px] opacity-60 mt-1 truncate">{task.title}</div>}
      </div>
      <Card>
        {error ? (
          <Empty>{error}</Empty>
        ) : !task ? (
          <Empty>Loading…</Empty>
        ) : !meta ? (
          <Empty>Unknown section.</Empty>
        ) : text ? (
          <pre className="whitespace-pre-wrap text-[13px] leading-relaxed opacity-90 font-sans">{text}</pre>
        ) : (
          <Empty>Nothing recorded.</Empty>
        )}
      </Card>
    </Page>
  );
}
