import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { SessionList } from "./pages/SessionList";
import { SessionDetail } from "./pages/SessionDetail";
import { NewSession } from "./pages/NewSession";
import { Settings } from "./pages/Settings";
import { WorkList } from "./pages/WorkList";
import { WorkTreeView } from "./pages/WorkTreeView";
import { AgentsAtWork } from "./pages/AgentsAtWork";
import "./index.css";

function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<SessionList />} />
        <Route path="/new" element={<NewSession />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/session/:sessionId" element={<SessionDetail />} />
        <Route path="/works" element={<WorkList />} />
        <Route path="/in-flight" element={<AgentsAtWork />} />
        <Route path="/work/:workId" element={<WorkTreeView />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
