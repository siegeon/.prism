import { useCallback, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { api } from "@/lib/api";
import { useProject, useProjectsListChange } from "@/lib/project";
// The nav owns the section names; the header renders whichever one is
// active. One source, so the two can never disagree.
import { sectionTitleFor } from "@/components/Sidebar";
import { Skeleton } from "@/components/ui";

type Me = { user?: { id: string; email: string; display_name: string } };

// Who you are signed in as (task fa52ba9e). After you claim your instance this
// shows your real name; before any identity work it shows the local owner.
function IdentityChip() {
  const [me, setMe] = useState<Me["user"] | null>(null);
  useEffect(() => {
    let cancel = false;
    api.get<Me>("/api/auth/me")
      .then((r) => { if (!cancel) setMe(r.user ?? null); })
      .catch(() => { if (!cancel) setMe(null); });
    return () => { cancel = true; };
  }, []);
  if (!me) return null;
  const name = me.display_name || me.email || "You";
  const initial = (name[0] || "?").toUpperCase();
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/60 pl-1.5 pr-3 py-1"
      title={me.email ? `Signed in as ${me.display_name || ""} (${me.email})` : "Signed in"}
    >
      <span className="grid h-6 w-6 place-items-center rounded-full bg-[color:var(--accent-teal-fg)] text-[color:var(--background-base)] text-[11px] font-bold">
        {initial}
      </span>
      <span className="text-xs text-[color:var(--text-secondary)] max-w-[160px] truncate">{name}</span>
    </span>
  );
}

export default function PageHeader() {
  const { pathname } = useLocation();
  const title = sectionTitleFor(pathname);
  const [connection, setConnection] = useState({ interrupted: false, attempt: 0 });
  const [project, setProject] = useProject();
  const [projects, setProjects] = useState<string[]>([]);
  // Gated on projects.length > 0 with projects=[] , the selector rendered
  // NOTHING pre-fetch and then popped in (task 89e90d1a).
  const [projectsLoaded, setProjectsLoaded] = useState(false);

  const loadProjects = useCallback(() => {
    api.get<{ projects: string[] }>("/api/projects")
      .then((r) => setProjects(r.projects))
      .catch(() => setProjects([]))
      .finally(() => setProjectsLoaded(true));
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);
  // Re-fetch when SettingsPage creates/deletes a project so the header
  // picker doesn't lag a page reload behind.
  useProjectsListChange(loadProjects);

  useEffect(() => {
    const onConnection = (event: Event) => {
      const detail = (event as CustomEvent<{ scope?: string; interrupted?: boolean; attempt?: number }>).detail;
      if (detail?.scope !== "workflows" || pathname !== "/workflows") return;
      setConnection({ interrupted: Boolean(detail.interrupted), attempt: detail.attempt ?? 0 });
    };
    window.addEventListener("prism:connection-state", onConnection);
    if (pathname !== "/workflows") setConnection({ interrupted: false, attempt: 0 });
    return () => window.removeEventListener("prism:connection-state", onConnection);
  }, [pathname]);

  return (
    <header role={connection.interrupted ? "status" : undefined} aria-live={connection.interrupted ? "polite" : undefined} className={`h-[80px] shrink-0 flex items-center justify-between px-8 border-b transition-colors ${connection.interrupted ? "border-amber-400/70 bg-amber-400/10" : "border-[color:var(--midground-base)]/10"}`}>
      <div className="flex items-center gap-3">
        {connection.interrupted && <span className="relative flex h-3 w-3" aria-hidden="true"><span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-40" /><span className="relative inline-flex h-3 w-3 rounded-full bg-amber-400" /></span>}
        <div>
          <h1 className={`text-[20px] font-[650] leading-tight tracking-[-0.01em] ${connection.interrupted ? "text-amber-300" : "text-[color:var(--text-primary)]"}`}>
            {connection.interrupted ? "Connection interrupted" : title}
          </h1>
          {connection.interrupted && <div className="mt-1 text-2xs uppercase tracking-wider text-amber-200/70">Workflows · reconnecting automatically · attempt {connection.attempt}</div>}
        </div>
      </div>
      <div className="flex items-center gap-4">
        <IdentityChip />
        {!projectsLoaded ? (
          <Skeleton className="h-[30px] w-[132px]" />
        ) : projects.length > 0 && (
          <label className="relative inline-flex items-center gap-2 text-2xs uppercase tracking-wider opacity-70">
            Project
            <div className="relative">
              <select
                value={project}
                onChange={(e) => setProject(e.target.value)}
                className="appearance-none bg-[color:var(--background-base)]/60 border border-[color:var(--midground-base)]/20 rounded-md pl-3 pr-8 py-1.5 text-[color:var(--midground-base)] text-xs uppercase tracking-wider focus:outline-none focus:border-[color:var(--midground-base)]/50"
              >
                {projects.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <ChevronDown className="w-3 h-3 absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none opacity-60" />
            </div>
          </label>
        )}
      </div>
    </header>
  );
}
