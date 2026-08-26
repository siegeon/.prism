import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  Activity, AppWindow, Bot, Brain, Eye, FolderTree, Inbox, Info,
  KeyRound, Layers, LayoutDashboard, ListChecks, MessageSquare, Plug,
  Radio, ScrollText, Search, Settings, Shapes, Sparkles, Workflow,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { Lozenge } from "@/components/Lozenge";
import { useProject } from "@/lib/project";
import { useScanActivity } from "@/lib/scan-activity";
import { useConductorState } from "@/lib/useConductorState";
import { currentTheme, toggleTheme } from "@/lib/theme";
import { useVersion, useVersionNotes } from "@/lib/version";
import { cn } from "@/lib/utils";

type StaleKey = "understand" | "graph" | "brain";

type Item = {
  to: string;
  label: string;
  icon: LucideIcon;
  staleKey?: StaleKey;
  // Inbox launch only (task 0784729f) — a one-off "NEW" chip, not a
  // reusable mechanism; no other item sets this.
  isNew?: boolean;
  // The Live nav item's icon only (owner live, 2026-08-24: "remove the
  // live pill and make the live icon in the activity view green") — a
  // one-off, not a reusable mechanism, same precedent as isNew above.
  isLiveIndicator?: boolean;
  // A one-line subtitle rendered under the label (task eca23a10, owner:
  // "the explore and the understand and the ontology under knowledge is
  // not super clear"). Only Knowledge's three items set this; every other
  // section is untouched.
  hint?: string;
};

type Section = { label?: string; items: Item[] };

// Always-visible top item. From inside Settings mode, clicking Dashboard
// is also the way back out, so it never disappears.
const TOP_SECTION: Section = {
  items: [
    { to: "/", label: "Dashboard", icon: LayoutDashboard },
  ],
};

// Default body: Knowledge + Activity, shown on every route that isn't
// /settings/*.
const MAIN_SECTIONS: Section[] = [
  // v6.0.7 — Memory belongs to Knowledge, not Activity. It is the
  // persisted record of patterns / conventions / failures / decisions
  // the team has learned, read by every future session via
  // memory_recall. Parallel to Brain (what code exists), Graph (how
  // code connects), Understand (architecture) — Memory is knowledge of
  // *how the team works*. Activity is reserved for things-in-motion.
  {
    label: "Knowledge",
    items: [
      // Knowledge is THREE surfaces (task eca23a10, superseding the prior
      // two-surface collapse at 89a1ddef): Brain is the Sigma graphvis (code
      // connections + search + context bundle; /graph + /explore redirect
      // here). Understand is the unified concept wiki — memory + OKF as one
      // OKF-style concept graph + read panel + links. Ontology is its own
      // surface now (moved out of Understand's toggle): classes/instances/
      // properties/axioms/rules/SPARQL. The old /memory and /okf entries
      // still fold into Understand (deep-linkable: /memory and /okf
      // redirect, /memory/:id preselects).
      { to: "/brain", label: "Explore", icon: Brain, staleKey: "brain", hint: "the code graph" },
      { to: "/understand", label: "Understand", icon: Eye, staleKey: "understand", hint: "concepts and memory" },
      { to: "/ontology", label: "Ontology", icon: Shapes, hint: "classes, rules, queries" },
    ],
  },
  {
    label: "Activity",
    items: [
      // Queue sits first, above Tasks, deliberately: signals arrive here
      // over their channel and become a task ONLY on the owner's word —
      // typed + clicked, never automatic (owner's model, mx-0889e4; task
      // 01d05bff). Supersedes the flag-gated /inbox item from task
      // d1854966, whose feature flag is now retired outright.
      { to: "/queue", label: "Queue", icon: Inbox },
      { to: "/tasks", label: "Tasks", icon: ListChecks },
      { to: "/conductor", label: "Conductor", icon: Workflow },
      // The walking-skeleton screen for "PRISM shows its work": live
      // agent activity flowing onto a node graph (task.changed,
      // drive.heartbeat, agent.run, tokens.turn over /sse/work).
      { to: "/live", label: "Live", icon: Radio, isLiveIndicator: true },
      // A workflow IS a bot: the conductor's FSM plus the role-carded agents
      // that drive it, wired on a canvas. `Workflow` already belongs to
      // Conductor above, so this wears `Bot` — the thing it actually shows.
      { to: "/workflows", label: "Workflows", icon: Bot },
      { to: "/retrievals", label: "Retrievals", icon: Search },
    ],
  },
  // Learning loop = the pipeline that PRODUCES new Memory entries.
  // Pipeline order, top-to-bottom: Sessions (raw) -> Consolidation
  // (briefs queued) -> Learning (scored output of completed reflections).
  {
    label: "Learning loop",
    items: [
      { to: "/sessions", label: "Sessions", icon: MessageSquare },
      { to: "/consolidation", label: "Consolidation", icon: Layers },
      { to: "/learning", label: "Learning", icon: Sparkles },
      // The file tree the ontology grammar resolves (task 5bfdf527): what
      // the loop's own outputs are FILED as, so it sits last — after the
      // pipeline that produces them.
      { to: "/files", label: "Files", icon: FolderTree },
    ],
  },
];

/** The active section's name, resolved from the SAME item arrays the nav
 * renders — one source, so the global header can never drift from the
 * sidebar. PageHeader used to keep its own parallel TITLES map, and it had
 * already gone stale: it said "Tasks" for /tasks while the nav said "Work".
 *
 * Longest matching route wins, so a detail page keeps its section's name
 * (/tasks/:id -> "Tasks"). A route with no nav item of its own falls back to
 * the label of the section that owns its sub-pages (/settings -> "Settings"),
 * and anything unrecognized falls back to the product name.
 */
export function sectionTitleFor(pathname: string): string {
  const sections = [TOP_SECTION, ...MAIN_SECTIONS, ...SETTINGS_SECTIONS];
  let best: Item | undefined;
  for (const section of sections) {
    for (const item of section.items) {
      if (pathname === item.to) return item.label;
      if (item.to !== "/" && pathname.startsWith(item.to + "/")
          && (!best || item.to.length > best.to.length)) {
        best = item;
      }
    }
  }
  if (best) return best.label;
  for (const section of sections) {
    if (section.label && section.items.some((i) => i.to.startsWith(pathname + "/"))) {
      return section.label;
    }
  }
  return "PRISM";
}

// Settings-mode body: when pathname starts with /settings, Knowledge +
// Activity collapse and Settings categories take their place. Each
// item routes to /settings/<id>; SettingsPage reads the URL param.
const SETTINGS_SECTIONS: Section[] = [
  {
    label: "Settings",
    items: [
      { to: "/settings/access-key", label: "Access key", icon: KeyRound },
      { to: "/settings/projects", label: "Projects", icon: FolderTree },
      // Connectors is the ONE home for every integration, Claude included.
      // Claude's own credentials and source registration live on its card
      // here, not behind a second nav entry — one subject, one door
      // (owner 2026-07-28, task c89edbeb).
      { to: "/settings/connectors", label: "Connectors", icon: Plug },
      { to: "/settings/activity", label: "Background activity", icon: Activity },
      { to: "/settings/logs", label: "Logs", icon: ScrollText },
      { to: "/settings/service", label: "Service", icon: Info },
    ],
  },
];


type Staleness = { understand: boolean; graph: boolean; brain: boolean };

function useStaleness(project: string): Staleness {
  const [stale, setStale] = useState<Staleness>({
    understand: false, graph: false, brain: false,
  });
  useEffect(() => {
    let cancel = false;
    const load = () => {
      api
        .get<Staleness>(`/api/staleness?project=${encodeURIComponent(project)}`)
        .then((s) => { if (!cancel) setStale(s); })
        .catch(() => { /* leave last good state */ });
    };
    // Only poll a tab someone is looking at (task c38ef597) — this ran every
    // 5s in every background tab. Refetch on focus so the badge is current
    // the moment it is seen.
    const tick = () => { if (!cancel && !document.hidden) load(); };
    load();
    const t = setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      cancel = true;
      clearInterval(t);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [project]);
  return stale;
}


export default function Sidebar() {
  const [project] = useProject();
  const stale = useStaleness(project);
  const scan = useScanActivity();
  const version = useVersion();
  // The Live nav icon's green pulse (formerly LiveBar's own shell card,
  // retired owner-live 2026-08-24). Single resolved source (task
  // 40c29b83) — Sidebar is now a third consumer of the same shared hook
  // ConductorPage already reads, so this can never contradict either
  // surface. Gated on `claimed` (task 2dfa94bd's fix) — an unclaimed
  // in_progress task must never paint this icon green.
  const { managed: liveManaged } = useConductorState(project);
  const isLive = liveManaged.some(
    (m) => m.claimed && (m.activity?.state === "working" || m.activity?.state === "driving"),
  );
  // The footer's hover tooltip is the one consumer of the full changelog —
  // fetched via the explicit `?notes=true` opt-in (task 842248bd), never
  // riding the lean default `useVersion()` response or its 15s poll. Task
  // d5465a25: the fetch is deferred until the row is actually hovered or
  // focused (wired below), not fired on every Sidebar mount.
  const { notes: versionNotes, ensureLoaded: loadVersionNotes } = useVersionNotes();
  const [theme, setTheme] = useState(currentTheme());
  const { pathname } = useLocation();
  const inSettings = pathname.startsWith("/settings");

  // Top item is always Dashboard. Below it: either Knowledge + Activity
  // (default) or the Settings categories (when in /settings/*). The
  // bottom Settings link stays in both modes — it's how you toggle in.
  const sections: Section[] = [
    TOP_SECTION,
    ...(inSettings ? SETTINGS_SECTIONS : MAIN_SECTIONS),
  ];

  return (
    <aside className="w-[240px] shrink-0 flex flex-col border-r border-[color:var(--nav-line)] bg-[color:var(--nav-bg)]">
      <div className="h-[80px] px-5 flex flex-col justify-center border-b border-[color:var(--nav-line)]">
        <div className="font-serif text-2xl leading-none tracking-tight text-[color:var(--nav-text-hi)]">PRISM</div>
        <div className="font-serif text-xl leading-none tracking-tight text-[color:var(--nav-text)] mt-1">SERVICE</div>
      </div>
      <nav className="flex-1 overflow-y-auto py-3">
        {sections.map((section, i) => (
          // Match the divider style used between Settings and the version
          // footer below — same border-default line + same py-3 spacing —
          // so all sidebar group breaks read as one consistent pattern.
          <div
            key={i}
            className={cn(
              i > 0 && "mt-3 pt-3 border-t border-[color:var(--nav-line)]",
            )}
          >
            {section.label && (
              <div className="px-5 mb-1 text-2xs uppercase tracking-[0.18em] text-[color:var(--nav-text)]">
                {section.label}
              </div>
            )}
            {section.items.map(({ to, label, icon: Icon, staleKey, isNew, isLiveIndicator, hint }) => {
              const isStale = staleKey ? stale[staleKey] : false;
              // While the drainer has work in flight, the surfaces it
              // populates (Brain, Graph, Understand — every item with a
              // staleKey) glow slate-blue. Blue takes precedence over the
              // amber stale dot because "actively scanning" implies
              // "stale is being addressed right now."
              const isScanning = staleKey ? scan.isActive : false;
              const isLiveNow = isLiveIndicator && isLive;
              return (
                <NavLink
                  key={to}
                  to={to}
                  end={to === "/"}
                  title={
                    isScanning
                      ? `${label} is being updated — drainer is running analyzers`
                      : isStale
                        ? `${label} is stale for project '${project}' — re-index needed`
                        : isLiveIndicator
                          ? (isLive ? "A claimed task is being driven right now" : "No task is being driven right now")
                          : undefined
                  }
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-3 px-5 py-2 text-[13px] uppercase tracking-wider transition-colors relative",
                      "text-[color:var(--nav-text)] hover:text-[color:var(--nav-text-hi)] hover:bg-[color:var(--nav-hover)]",
                      isActive && "text-[color:var(--nav-active-text)] bg-[color:var(--nav-active-bg)] font-semibold",
                      // Soft slate-blue glow + animated pulse on the
                      // row itself while scanning. Subtle enough to
                      // not fight the active-route indicator, loud
                      // enough that the user knows something is alive.
                      isScanning && "text-sky-200 bg-sky-500/[0.06] animate-pulse",
                    )
                  }
                >
                  <Icon
                    className={cn(
                      "w-4 h-4",
                      isLiveNow && "text-[color:var(--accent-sage-fg)] animate-pulse",
                    )}
                  />
                  <span className="flex-1 min-w-0">
                    <span className="block">{label}</span>
                    {hint && (
                      <span className="block normal-case tracking-normal text-2xs text-[color:var(--nav-text)] opacity-70">
                        {hint}
                      </span>
                    )}
                  </span>
                  {isNew && <Lozenge tone="new">New</Lozenge>}
                  {isScanning ? (
                    <span
                      className="w-2 h-2 rounded-full bg-sky-300 shadow-[0_0_8px_3px_rgba(125,211,252,0.6)]"
                      aria-label="scanning"
                    />
                  ) : isStale ? (
                    <span
                      className="w-2 h-2 rounded-full bg-[color:var(--accent-amber-fg)] shadow-[0_0_6px_2px_rgba(251,191,36,0.5)]"
                      aria-label="stale"
                    />
                  ) : null}
                </NavLink>
              );
            })}
          </div>
        ))}
      </nav>
      <div className="border-t border-[color:var(--nav-line)]">
        {/* Footer toggle. In default mode it's "Settings" and enters
            the Settings sidebar mode. In Settings mode it flips to
            "Application" with a different icon so the user has a
            clear, single-click way back to the main app. We DON'T
            use NavLink active styling here because the active route
            is already reflected by the items in the Settings nav
            above — keeping this neutral makes it read as a mode
            toggle, not yet-another-link. */}
        {inSettings ? (
          <NavLink
            to="/"
            className={cn(
              "flex items-center gap-3 px-5 py-3 text-[13px] uppercase tracking-wider transition-colors",
              "text-[color:var(--nav-text)] hover:text-[color:var(--nav-text-hi)] hover:bg-[color:var(--nav-hover)]",
            )}
            title="Return to the main application"
          >
            <AppWindow className="w-4 h-4" />
            <span>Application</span>
          </NavLink>
        ) : (
          <NavLink
            to="/settings"
            className={cn(
              "flex items-center gap-3 px-5 py-3 text-[13px] uppercase tracking-wider transition-colors",
              "text-[color:var(--nav-text)] hover:text-[color:var(--nav-text-hi)] hover:bg-[color:var(--nav-hover)]",
            )}
            title="Open application settings"
          >
            <Settings className="w-4 h-4" />
            <span>Settings</span>
          </NavLink>
        )}
        <div
          className="px-5 py-3 text-2xs uppercase tracking-wider text-[color:var(--nav-text)] border-t border-[color:var(--nav-line)] flex items-center gap-2"
          title={versionNotes}
          onMouseEnter={loadVersionNotes}
          onFocus={loadVersionNotes}
        >
          <button
            type="button"
            onClick={() => setTheme(toggleTheme())}
            className="inline-flex items-center gap-1 hover:text-[color:var(--nav-text-hi)] transition-colors"
            title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            ◐ <span className="sr-only">toggle theme</span>
          </button>
          <span>v{version?.version ?? "…"}</span>
          {version?.dev_mode && (
            <span
              className="inline-flex items-center px-1.5 py-0.5 rounded-sm text-2xs font-bold tracking-widest bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] border border-[color:var(--accent-amber-ring)]"
              title="Source-run instance (PRISM_DEV_MODE=1)"
            >
              DEV
            </span>
          )}
        </div>
      </div>
    </aside>
  );
}
