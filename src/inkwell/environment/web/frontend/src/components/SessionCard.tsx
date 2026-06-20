import { Link } from "react-router-dom";
import type { SessionSummary } from "../types";
import { stageLabel } from "../types";

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.round(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${s}s`;
}

function formatSessionTitle(createdAt: string): string {
  const d = new Date(createdAt);
  if (isNaN(d.getTime())) return createdAt;
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function statusLabel(session: SessionSummary): string {
  if (session.status === "running" && session.stage === "complete") return "standby";
  if (session.status === "interrupted") {
    return `interrupted at ${stageLabel(session.stage)}`;
  }
  if (session.status === "paused") {
    return `paused after ${stageLabel(session.stage)}`;
  }
  return session.status;
}

export function SessionCard({ session }: { session: SessionSummary }) {
  return (
    <Link to={`/session/${session.session_id}`} className="session-card">
      <div className="session-card-header">
        <span className="session-title">{session.title || formatSessionTitle(session.created_at) || session.session_id}</span>
        <span className={`status-badge status-${session.status}`}>
          {statusLabel(session)}
        </span>
      </div>
      <div className="session-card-body">
        {session.created_at && <span>{formatSessionTitle(session.created_at)}</span>}
        <span className="profile-badge">{session.profile}</span>
        {session.cost_usd > 0 && <span>${session.cost_usd.toFixed(2)}</span>}
        {session.duration_s > 0 && <span>{formatDuration(session.duration_s)}</span>}
        {session.doc_url && <span className="doc-link-hint">Has doc</span>}
      </div>
    </Link>
  );
}
