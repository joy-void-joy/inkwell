import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchEverythingInFlight } from "../api/client";
import type { PartInFlight } from "../types";
import { InFlightPart } from "../components/InFlight";

// Every part of every work being written right now, oldest first.
//
// One page for the question an author with two books open actually has — what
// is being worked on — rather than one work page per book, each to be visited
// in turn and each reporting only its own. Oldest first because a run that has
// been going longest is the one worth looking at: it is either the biggest
// part or the one that is stuck.

const POLL_MS = 5000;

export function AgentsAtWork() {
  const [parts, setParts] = useState<PartInFlight[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const found = await fetchEverythingInFlight();
        if (!live) return;
        setParts(found);
        setError("");
      } catch (failure) {
        if (live) setError(String(failure));
      } finally {
        if (live) setLoading(false);
      }
    };
    load();
    const interval = setInterval(load, POLL_MS);
    return () => {
      live = false;
      clearInterval(interval);
    };
  }, []);

  const orphaned = parts.filter((part) => part.status === "orphaned").length;

  return (
    <div className="page">
      <div className="page-header">
        <h1>Being written now</h1>
        <Link to="/works" className="back-link">
          Works
        </Link>
      </div>

      {error && <p className="run-error">{error}</p>}

      {loading ? (
        <p>Loading...</p>
      ) : parts.length === 0 ? (
        <p>
          Nothing is being written. Open a work to start a pass over it, or one
          part of it.
        </p>
      ) : (
        <>
          <p>
            {parts.length} part(s) in flight
            {orphaned > 0 &&
              ` · ${orphaned} held by a run that is gone — clear those on the work's page`}
          </p>
          {parts.map((part) => (
            <InFlightPart
              key={`${part.work}/${part.key}`}
              part={part}
              naming
            />
          ))}
        </>
      )}
    </div>
  );
}
