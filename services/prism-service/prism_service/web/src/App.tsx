import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "@/components/Sidebar";
import PageHeader from "@/components/PageHeader";
import Backdrop from "@/components/Backdrop";
import LiveStatusStrip from "@/components/LiveStatusStrip";
import DashboardPage from "@/pages/DashboardPage";
// The unified Brain surface (graph connections + search + context bundle).
// Lives in ExplorePage.tsx; it replaced the old separate Brain + Graph pages.
import ExplorePage from "@/pages/ExplorePage";
import MemoryPage from "@/pages/MemoryPage";
import MemoryDetailPage from "@/pages/MemoryDetailPage";
import TasksPage from "@/pages/TasksPage";
import TaskDetailPage from "@/pages/TaskDetailPage";
import ConductorPage from "@/pages/ConductorPage";
import SessionsPage from "@/pages/SessionsPage";
import RetrievalsPage from "@/pages/RetrievalsPage";
import LearningPage from "@/pages/LearningPage";
import ConsolidationPage from "@/pages/ConsolidationPage";
import UnderstandPage from "@/pages/UnderstandPage";
import SettingsPage from "@/pages/SettingsPage";

export default function App() {
  return (
    <div className="h-full w-full flex bg-[color:var(--background-base)] text-[color:var(--midground-base)] relative">
      <Backdrop />
      <Sidebar />
      <main className="flex-1 flex flex-col min-w-0">
        <LiveStatusStrip />
        <PageHeader />
        <div className="flex-1 overflow-y-auto">
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            {/* Brain = the one place to explore the knowledge. /graph and
                the old /explore redirect here so nothing breaks. */}
            <Route path="/brain" element={<ExplorePage />} />
            <Route path="/explore" element={<Navigate to="/brain" replace />} />
            <Route path="/graph" element={<Navigate to="/brain" replace />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/memory/:id" element={<MemoryDetailPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/tasks/:id" element={<TaskDetailPage />} />
            <Route path="/conductor" element={<ConductorPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/retrievals" element={<RetrievalsPage />} />
            <Route path="/learning" element={<LearningPage />} />
            <Route path="/consolidation" element={<ConsolidationPage />} />
            <Route path="/understand" element={<UnderstandPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/settings/:section" element={<SettingsPage />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
