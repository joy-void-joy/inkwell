import { Link } from "react-router-dom";
import type { PartInFlight } from "../types";
import { stageLabel } from "../types";

// What is being written right now, which is the one thing a work page could
// not say. A pass over a book reports when it is over, so an author who
// started one had a spinner and a settled-looking tree for however long it
// took — the run doing the work was reachable only by finding the right row
// among two hundred, and only if they knew to look there.

function elapsed(since: string): string {
  const began = new Date(since).getTime();
  if (isNaN(began)) return "";
  const seconds = Math.max(0, (Date.now() - began) / 1000);
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

// An orphaned part is the one worth reading twice: it is leased by a run that
// is gone, so it looks exactly like work in progress and will never finish.
function standing(part: PartInFlight): string {
  if (part.status === "orphaned") return "no run is honouring this lease";
  const stage = part.stage ? stageLabel(part.stage) : "starting";
  return part.status === "running" ? stage : `${part.status} at ${stage}`;
}

export function InFlightPart({
  part,
  naming,
}: {
  part: PartInFlight;
  naming?: boolean;
}) {
  return (
    <div className={`in-flight-part in-flight-${part.status}`}>
      <span className="in-flight-key">
        {naming && <span className="in-flight-work">{part.work} </span>}
        {part.key}
      </span>{" "}
      <span className="in-flight-title">{part.title}</span>
      <div className="in-flight-detail">
        {standing(part)} · {elapsed(part.since)}
        {part.cost_usd > 0 && ` · $${part.cost_usd.toFixed(2)}`}{" "}
        <Link to={`/session/${part.session}`} title="What this run is doing">
          run {part.session.slice(0, 8)}
        </Link>
        {part.doc_url && (
          <>
            {" · "}
            <a href={part.doc_url} target="_blank" rel="noreferrer">
              document
            </a>
          </>
        )}
        {part.reason && <div className="in-flight-reason">{part.reason}</div>}
      </div>
    </div>
  );
}

export function InFlight({
  parts,
  naming,
}: {
  parts: PartInFlight[];
  naming?: boolean;
}) {
  if (parts.length === 0) return null;
  return (
    <section className="in-flight">
      <h2>Being written now ({parts.length})</h2>
      {parts.map((part) => (
        <InFlightPart
          key={`${part.work}/${part.key}`}
          part={part}
          naming={naming}
        />
      ))}
    </section>
  );
}
