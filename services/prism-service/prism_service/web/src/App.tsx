import { useEffect, lazy, Suspense } from "react";
import { Routes, Route, Navigate, useLocation, useParams } from "react-router-dom";
import { AnimatePresence } from "motion/react";
import { resolveInitialProject } from "@/lib/project";
import Sidebar from "@/components/Sidebar";
import PageHeader from "@/components/PageHeader";
import Backdrop from "@/components/Backdrop";
import LiveStatusStrip from "@/components/LiveStatusStrip";
import LiveBar from "@/components/LiveBar";

// Route-level code splitting (v6.3.40). Every page used to be a static import,
// so the graph/Sigma, conductor animation, settings, and mermaid-adjacent code
// all landed in one ~1.6MB main chunk that had to parse before anything
// rendered. Each page is now its own chunk fetched on navigation; the initial
// load is just the shell + the landing dashboard. DashboardPage stays eager
// because it's the default route — lazy-loading it would only add a flash.
import DashboardPage from "@/pages/DashboardPage";
const ExplorePage = lazy(() => import("@/pages/ExplorePage"));
const TasksPage = lazy(() => import("@/pages/TasksPage"));
const CompletedTasksPage = lazy(() => import("@/pages/CompletedTasksPage"));
const TaskDetailPage = lazy(() => import("@/pages/TaskDetailPage"));
const TaskTextPage = lazy(() => import("@/pages/TaskTextPage"));
const ConductorPage = lazy(() => import("@/pages/ConductorPage"));
const SessionsPage = lazy(() => import("@/pages/SessionsPage"));
const SessionDetailPage = lazy(() => import("@/pages/SessionDetailPage"));
const RetrievalsPage = lazy(() => import("@/pages/RetrievalsPage"));
const LearningPage = lazy(() => import("@/pages/LearningPage"));
const ConsolidationPage = lazy(() => import("@/pages/ConsolidationPage"));
const UnderstandPage = lazy(() => import("@/pages/UnderstandPage"));
const ArtifactPage = lazy(() => import("@/pages/ArtifactPage"));
const SettingsPage = lazy(() => import("@/pages/SettingsPage"));

export default function App() {
  // Route-swap transition: AnimatePresence mode="wait" keyed on the pathname
  // lets the leaving page finish its exit before the next mounts. We pass an
  // explicit `location` to <Routes> so AnimatePresence sees a stable tree to
  // animate out. The single flex-1 overflow-y-auto scroll container is
  // preserved as the wrapper, and the two <Navigate> redirects are untouched.
  const location = useLocation();
  // Cold-start resolver (v6.3.23): on first mount with no persisted project
  // and no ?project= deep-link, land on the busiest non-'default' project so
  // /conductor opens on real work instead of the empty 'default' blank state.
  useEffect(() => { void resolveInitialProject(); }, []);
  return (
    <div className="h-full w-full flex bg-[color:var(--background-base)] text-[color:var(--midground-base)] relative">
      <Backdrop />
      <Sidebar />
      <main className="flex-1 flex flex-col min-w-0">
        <LiveStatusStrip />
        <PageHeader />
        {/* Conductor pulse — persistent across navigation, honest idle state.
            Distinct from LiveStatusStrip (the analyzer scan-queue strip). */}
        <LiveBar />
        <div className="flex-1 overflow-y-auto">
          <AnimatePresence mode="wait">
          <Suspense
            fallback={<div className="p-8 text-sm opacity-50">Loading…</div>}
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
            <Route path="/tasks" element={<TasksPage />} />
            {/* Completed work lives on its own surface, off the active board
                (feedback: done-tasks-off-board). Static segment ranks above
                /tasks/:id so it never resolves as a task detail. */}
            <Route path="/tasks/completed" element={<CompletedTasksPage />} />
            <Route path="/tasks/:id" element={<TaskDetailPage />} />
            <Route path="/tasks/:id/:section" element={<TaskTextPage />} />
            <Route path="/conductor" element={<ConductorPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/sessions/:id" element={<SessionDetailPage />} />
            <Route path="/retrievals" element={<RetrievalsPage />} />
            <Route path="/learning" element={<LearningPage />} />
            <Route path="/consolidation" element={<ConsolidationPage />} />
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
  );
}

// /memory/:id deep-links now resolve a concept in the unified Understand wiki:
// redirect to /understand?concept=:id so the wiki preselects that node.
function MemoryConceptRedirect() {
  const { id = "" } = useParams<{ id: string }>();
  return <Navigate to={`/understand?concept=${encodeURIComponent(id)}`} replace />;
}
