import { useEffect, useState, lazy, Suspense, type ComponentType } from "react";
import { Routes, Route, Navigate, useLocation, useParams } from "react-router-dom";
import { AnimatePresence } from "motion/react";
import { resolveInitialProject } from "@/lib/project";
import { api, ApiError } from "@/lib/api";
import ClaimPage from "@/pages/ClaimPage";
import KeyGatePage from "@/pages/KeyGatePage";
import Sidebar, { INBOX_ENABLED } from "@/components/Sidebar";
import PageHeader from "@/components/PageHeader";
import Backdrop from "@/components/Backdrop";
import LiveStatusStrip from "@/components/LiveStatusStrip";
import { Skeleton } from "@/components/ui";
import { AgentBridgeProvider } from "@/lib/agentBridge";
import ReconnectBanner from "@/components/ReconnectBanner";
import { waitForServerThenReload } from "@/lib/reconnect";

// Route-level code splitting (v6.3.40). Every page used to be a static import,
// so the graph/Sigma, conductor animation, settings, and mermaid-adjacent code
// all landed in one ~1.6MB main chunk that had to parse before anything
// rendered. Each page is now its own chunk fetched on navigation; the initial
// load is just the shell + the landing dashboard. DashboardPage stays eager
// because it's the default route — lazy-loading it would only add a flash.
import DashboardPage from "@/pages/DashboardPage";

/** A watched production build replaces hashed route chunks. An already-open
 * tab may request yesterday's hash before the version poll can reload it;
 * recover once instead of leaving the root Suspense boundary permanently
 * blank. A second failure is real and is rethrown for diagnostics.
 *
 * NEVER reload blind (owner live, 2026-08-24: "it should NEVER go white").
 * A chunk import can fail for two very different reasons — a stale build
 * hash (server IS up, just serving a newer manifest; reloading fixes it
 * instantly) or the server being genuinely unreachable mid-restart
 * (reloading immediately just fails the SAME way, and a failed top-level
 * navigation shows the BROWSER's own blank error page, which nothing in
 * this app can intercept). waitForServerThenReload() only ever reloads
 * after a real probe succeeds, showing ReconnectBanner in the meantime —
 * the current page's own JS keeps running throughout, so the tab never
 * goes blank either way. */
function lazyRoute<T extends { default: ComponentType }>(key: string, loader: () => Promise<T>) {
  return lazy(async () => {
    const reloadKey = `prism.chunk-reload.${key}`;
    try {
      const module = await loader();
      sessionStorage.removeItem(reloadKey);
      return module;
    } catch (error) {
      if (!sessionStorage.getItem(reloadKey)) {
        sessionStorage.setItem(reloadKey, "1");
        waitForServerThenReload();
        return new Promise<T>(() => { /* navigation continues once the server answers */ });
      }
      throw error;
    }
  });
}

const ExplorePage = lazyRoute("explore", () => import("@/pages/ExplorePage"));
const InboxPage = lazyRoute("inbox", () => import("@/pages/InboxPage"));
const TasksPage = lazyRoute("tasks", () => import("@/pages/TasksPage"));
const CompletedTasksPage = lazyRoute("completed", () => import("@/pages/CompletedTasksPage"));
const TaskDetailPage = lazyRoute("task-detail", () => import("@/pages/TaskDetailPage"));
const TaskTextPage = lazyRoute("task-text", () => import("@/pages/TaskTextPage"));
const ConductorPage = lazyRoute("conductor", () => import("@/pages/ConductorPage"));
const LivePage = lazyRoute("live", () => import("@/pages/LivePage"));
const WorkflowsPage = lazyRoute("workflows", () => import("@/pages/WorkflowsPage"));
const SessionsPage = lazyRoute("sessions", () => import("@/pages/SessionsPage"));
const SessionDetailPage = lazyRoute("session-detail", () => import("@/pages/SessionDetailPage"));
const RetrievalsPage = lazyRoute("retrievals", () => import("@/pages/RetrievalsPage"));
const LearningPage = lazyRoute("learning", () => import("@/pages/LearningPage"));
const ConsolidationPage = lazyRoute("consolidation", () => import("@/pages/ConsolidationPage"));
const FilesPage = lazyRoute("files", () => import("@/pages/FilesPage"));
const UnderstandPage = lazyRoute("understand", () => import("@/pages/UnderstandPage"));
const ArtifactPage = lazyRoute("artifact", () => import("@/pages/ArtifactPage"));
const SettingsPage = lazyRoute("settings", () => import("@/pages/SettingsPage"));

export default function App() {
  // Route-swap transition: AnimatePresence mode="wait" keyed on the pathname
  // lets the leaving page finish its exit before the next mounts. We pass an
  // explicit `location` to <Routes> so AnimatePresence sees a stable tree to
  // animate out. The single flex-1 overflow-y-auto scroll container is
  // preserved as the wrapper, and the two <Navigate> redirects are untouched.
  const location = useLocation();
  // Bumped after a successful sign-in so the boot probe re-runs with the key.
  const [authAttempt, setAuthAttempt] = useState(0);
  // CLAIM GATE (task fa52ba9e): the first time PRISM opens after auto-updating
  // from a credential-free build, the instance is UNCLAIMED — show only the
  // claim screen until the existing owner claims it. `null` = still checking.
  const [claimed, setClaimed] = useState<boolean | null>(null);
  // REMOTE SIGN-IN (task 4367c12f): reached across the network, the server
  // answers 401 until this browser proves the owner's access key. That is the
  // ONLY thing that raises this flag — on the machine running PRISM the server
  // trusts loopback and nobody is ever asked.
  const [needsKey, setNeedsKey] = useState(false);
  useEffect(() => {
    let cancel = false;
    api.get<{ claimed: boolean }>("/api/auth/claim-status")
      .then((s) => { if (!cancel) { setNeedsKey(false); setClaimed(!!s.claimed); } })
      .catch((err) => {
        if (cancel) return;
        // A 401 is not a glitch, it is the server asking who you are.
        if (err instanceof ApiError && err.status === 401) {
          setNeedsKey(true);
          setClaimed(true);
          return;
        }
        // Anything else fails OPEN so a status glitch never locks the owner
        // out of their own instance (data safety over a gate).
        setClaimed(true);
      });
    return () => { cancel = true; };
  }, [authAttempt]);
  // Cold-start resolver (v6.3.23): on first mount with no persisted project
  // and no ?project= deep-link, land on the busiest non-'default' project so
  // /conductor opens on real work instead of the empty 'default' blank state.
  // Must wait for a SIGNED-IN state, not merely a claimed one. Signing in
  // remotely does not change `claimed` (the key gate already set it true), so
  // keying this on `claimed` alone left the resolver's 401'd first attempt as
  // the only one — and the app landed on the empty 'default' project looking
  // exactly like the blank dashboard this whole change set out to fix.
  useEffect(() => {
    if (claimed && !needsKey) void resolveInitialProject();
  }, [claimed, needsKey, authAttempt]);

  if (claimed === null) {
    return <div className="h-full w-full bg-[color:var(--background-base)]" />;
  }
  // Ahead of the claim gate: without a credential the server will not tell us
  // whether the instance is claimed, so there is nothing to decide yet.
  if (needsKey) {
    return <KeyGatePage onAuthed={() => setAuthAttempt((n) => n + 1)} />;
  }
  if (!claimed) {
    return <ClaimPage onClaimed={() => setClaimed(true)} />;
  }
  return (
    <AgentBridgeProvider>
    <div className="h-full w-full flex flex-col bg-[color:var(--background-base)] text-[color:var(--midground-base)] relative">
      {/* Spans the FULL shell width, above Sidebar/main both — a system
          notification, not something scoped to one panel (owner live,
          2026-08-24: "it should NEVER go white... it should have the
          banner letting the customer know it's updating"). */}
      <ReconnectBanner />
      <div className="flex-1 flex min-h-0">
      <Backdrop />
      <Sidebar />
      <main className="flex-1 flex flex-col min-w-0">
        <LiveStatusStrip />
        <PageHeader />
        {/* Conductor pulse now lives on Sidebar's own LIVE nav icon (owner
            live, 2026-08-24: "remove the live pill and make the live icon
            in the activity view green") — no shell-level pulse card here
            anymore. Distinct from LiveStatusStrip (the analyzer scan-queue
            strip), which stays. */}
        <div className="flex-1 overflow-y-auto">
          <AnimatePresence mode="wait">
          <Suspense
            fallback={
              // Route-level fallback (task c3f4cf12): shared by every lazy
              // route (/tasks, /tasks/:id, /conductor, ...) so a cold-cache
              // navigation paints a page-shaped placeholder instead of a
              // bare one-line text node — a header row plus card-shaped
              // rows, composed from the existing Skeleton primitive
              // (ui.tsx:172).
              <div aria-hidden className="p-8 space-y-6 w-full min-w-[720px]">
                <Skeleton className="h-8 w-64" />
                <div className="space-y-3">
                  <Skeleton className="h-24 w-full" />
                  <Skeleton className="h-24 w-full" />
                  <Skeleton className="h-24 w-full" />
                </div>
              </div>
            }
          >
          <Routes location={location} key={location.pathname}>
            <Route path="/" element={<DashboardPage />} />
            {/* Brain = the one place to explore the knowledge. /graph and
                the old /explore redirect here so nothing breaks. */}
            <Route path="/brain" element={<ExplorePage />} />
            <Route path="/explore" element={<Navigate to={{ pathname: "/brain", search: location.search }} replace />} />
            <Route path="/graph" element={<Navigate to="/brain" replace />} />
            {/* Knowledge collapsed to TWO surfaces (task 89a1ddef): Brain
                (Sigma graphvis) + the unified Understand wiki. /memory and
                /okf fold into Understand but stay deep-linkable via redirect;
                /memory/:id preselects that concept node in the wiki. */}
            <Route path="/memory" element={<Navigate to="/understand" replace />} />
            <Route
              path="/memory/:id"
              element={<MemoryConceptRedirect />}
            />
            <Route path="/okf" element={<Navigate to="/understand" replace />} />
            {/* Hidden behind INBOX_ENABLED while under development (task
                d1854966) — a typed/bookmarked /inbox redirects to the
                Dashboard until the flag flips; InboxPage stays imported
                and wired for that flip. */}
            <Route path="/inbox" element={INBOX_ENABLED ? <InboxPage /> : <Navigate to="/" replace />} />
            <Route path="/tasks" element={<TasksPage />} />
            {/* Completed work lives on its own surface, off the active board
                (feedback: done-tasks-off-board). Static segment ranks above
                /tasks/:id so it never resolves as a task detail. */}
            <Route path="/tasks/completed" element={<CompletedTasksPage />} />
            <Route path="/tasks/:id" element={<TaskDetailPage />} />
            <Route path="/tasks/:id/:section" element={<TaskTextPage />} />
            <Route path="/conductor" element={<ConductorPage />} />
            <Route path="/live" element={<LivePage />} />
            <Route path="/workflows" element={<WorkflowsPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/sessions/:id" element={<SessionDetailPage />} />
            <Route path="/retrievals" element={<RetrievalsPage />} />
            <Route path="/learning" element={<LearningPage />} />
            <Route path="/consolidation" element={<ConsolidationPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/understand" element={<UnderstandPage />} />
            {/* Unified artifact surface (xref S5): the single destination a
                resolved CODE token routes to. A ROUTE, not a nav entry. */}
            <Route path="/artifact" element={<ArtifactPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/settings/:section" element={<SettingsPage />} />
          </Routes>
          </Suspense>
          </AnimatePresence>
        </div>
      </main>
      </div>
    </div>
    </AgentBridgeProvider>
  );
}

// /memory/:id deep-links now resolve a concept in the unified Understand wiki:
// redirect to /understand?concept=:id so the wiki preselects that node.
function MemoryConceptRedirect() {
  const { id = "" } = useParams<{ id: string }>();
  return <Navigate to={`/understand?concept=${encodeURIComponent(id)}`} replace />;
}
