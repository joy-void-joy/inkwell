import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listSessions } from "../api/client";
import { SessionCard } from "../components/SessionCard";
import type { SessionSummary } from "../types";

export function SessionList() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        const data = await listSessions();
        setSessions(data);
      } finally {
        setLoading(false);
      }
    };
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  const running = sessions.filter((s) => s.status === "running");
  const rest = sessions.filter((s) => s.status !== "running");

  return (
    <div className="page session-list">
      <div className="page-header">
        <h1>Sessions</h1>
        <div style={{ display: "flex", gap: "8px" }}>
          <Link to="/settings" className="back-link" style={{ padding: "6px 14px" }}>
            Settings
          </Link>
          <Link to="/new" className="btn-primary">
            New Session
          </Link>
        </div>
      </div>

      {loading && <p>Loading...</p>}

      {running.length > 0 && (
        <section>
          <h2>Running</h2>
          <div className="session-grid">
            {running.map((s) => (
              <SessionCard key={s.session_id} session={s} />
            ))}
          </div>
        </section>
      )}

      {rest.length > 0 && (
        <section>
          <div className="session-grid">
            {rest.map((s) => (
              <SessionCard key={s.session_id} session={s} />
            ))}
          </div>
        </section>
      )}

      {!loading && sessions.length === 0 && (
        <p className="empty-state">
          No sessions yet.{" "}
          <Link to="/new">Start one.</Link>
        </p>
      )}
    </div>
  );
}
