import { useCallback, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { api } from "@/lib/api";
import { useProject, useProjectsListChange } from "@/lib/project";
import { Skeleton } from "@/components/ui";

const TITLES: Record<string, string> = {
  "/": "Dashboard", "/brain": "Explore", "/graph": "Graph",
  "/memory": "Memory", "/tasks": "Tasks", "/conductor": "Conductor",
  "/sessions": "Sessions", "/retrievals": "Retrievals",
  "/learning": "Learning", "/consolidation": "Consolidation",
  "/understand": "Understand", "/settings": "Settings",
};

// v6.0.15 — match nested routes (e.g. /tasks/:id -> "Tasks",
// /settings/:section -> "Settings") by longest-prefix lookup so the
// header doesn't fall back to the generic "PRISM" on detail pages.
function resolveTitle(pathname: string): string {
  if (TITLES[pathname]) return TITLES[pathname];
  const candidates = Object.keys(TITLES).sort((a, b) => b.length - a.length);
  for (const prefix of candidates) {
    if (prefix !== "/" && pathname.startsWith(prefix + "/")) return TITLES[prefix];
  }
  return "PRISM";
}

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
  const title = resolveTitle(pathname);
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

  return (
    <header className="h-[80px] shrink-0 flex items-center justify-between px-8 border-b border-[color:var(--midground-base)]/10">
      <h1 className="text-[20px] font-[650] leading-tight tracking-[-0.01em] text-[color:var(--text-primary)]">
        {title}
      </h1>
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
