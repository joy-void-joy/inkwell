import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchWorks } from "../api/client";
import type { WorkSummary } from "../types";

// A work is not a session and is listed apart from one. A session has a
// beginning and an end; a work outlives every run against it and is what an
// author comes back to.

const POLL_MS = 10000;

export function WorkList() {
  const [works, setWorks] = useState<WorkSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const load = async () => {
      try {
        setWorks(await fetchWorks());
      } finally {
        setLoading(false);
      }
    };
    load();
    const interval = setInterval(load, POLL_MS);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="page work-list">
      <div className="page-header">
        <h1>Works</h1>
        <Link to="/" className="back-link">
          Sessions
        </Link>
      </div>

      {loading && <p>Loading...</p>}

      {!loading && works.length === 0 && (
        <p>
          No work is recorded yet. Import one with{" "}
          <code>lup-devtools manuscript import &lt;chapters&gt; --work &lt;slug&gt;</code>.
        </p>
      )}

      {works.map((work) => (
        <Link key={work.id} to={`/work/${work.id}`} className="work-card">
          <strong>{work.title}</strong>
          <span>
            {work.parts} part(s) ·{" "}
            {work.outstanding === 0
              ? "settled"
              : `${work.outstanding} outstanding`}
          </span>
        </Link>
      ))}
    </div>
  );
}
