import { useState } from "react";
import { previewWorkRun, startWorkRun, stopWorkRun } from "../api/client";
import { stalenessLabel } from "../types";
import type { WorkLoopStatus, WouldRun } from "../types";

// Starting a loop is the one thing on this page that spends money, and how much
// depends on how many parts are outstanding — each is a whole pipeline run. So
// the preview is not a convenience here, it is the step that makes the start
// button a decision: nothing can be run until the author has seen what would
// run. Everything else on the panel is about being able to stop.

interface Props {
  workId: string;
  loop: WorkLoopStatus | null;
  rooted: boolean;
  onChanged: () => void;
}

const PASS_DEFAULT = 5;
const CONCURRENCY_DEFAULT = 4;

export function RunPanel({ workId, loop, rooted, onChanged }: Props) {
  const [would, setWould] = useState<WouldRun | null>(null);
  const [passes, setPasses] = useState(PASS_DEFAULT);
  const [limit, setLimit] = useState(1);
  const [concurrency, setConcurrency] = useState(CONCURRENCY_DEFAULT);
  const [reconcile, setReconcile] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const running = loop?.running ?? false;

  const attempt = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      onChanged();
    } catch (failure) {
      setError(String(failure));
    } finally {
      setBusy(false);
    }
  };

  const preview = () =>
    attempt(async () => setWould(await previewWorkRun(workId, limit)));

  const start = () =>
    attempt(async () => {
      await startWorkRun(workId, { passes, limit, concurrency, reconcile });
      setWould(null);
    });

  const stop = () => attempt(() => stopWorkRun(workId));

  return (
    <section className="run-panel">
      <h2>Run the loop</h2>

      {!rooted && (
        <p className="run-blocked">
          This work was imported from a directory that is not there, so nothing
          can run against it. Re-import it to pick up where its text lives now.
        </p>
      )}

      <div className="run-settings">
        <label>
          Parts per pass
          <input
            type="number"
            min={0}
            value={limit}
            disabled={running}
            onChange={(event) => setLimit(Number(event.target.value))}
          />
          <small>0 runs every outstanding part</small>
        </label>
        <label>
          Passes at most
          <input
            type="number"
            min={1}
            value={passes}
            disabled={running}
            onChange={(event) => setPasses(Number(event.target.value))}
          />
        </label>
        <label>
          At once
          <input
            type="number"
            min={1}
            value={concurrency}
            disabled={running}
            onChange={(event) => setConcurrency(Number(event.target.value))}
          />
        </label>
        <label className="run-checkbox">
          <input
            type="checkbox"
            checked={reconcile}
            disabled={running}
            onChange={(event) => setReconcile(event.target.checked)}
          />
          Read each wave's rewrites against each other
        </label>
      </div>

      <div className="run-actions">
        <button onClick={preview} disabled={busy || running || !rooted}>
          What would run?
        </button>
        <button
          className="run-start"
          onClick={start}
          disabled={busy || running || !would || would.parts.length === 0}
        >
          {would
            ? `Run ${would.parts.length} part(s)`
            : "Run — preview it first"}
        </button>
        {running && (
          <button onClick={stop} disabled={busy}>
            Stop
          </button>
        )}
      </div>

      {error && <p className="run-error">{error}</p>}

      {would && (
        <div className="run-preview">
          <p>
            {would.parts.length === 0
              ? "Nothing would run — the work is settled."
              : `${would.parts.length} part(s) would run this pass, of ${would.outstanding} outstanding. Each is a full pipeline run.`}
          </p>
          {would.parts.map((part) => (
            <div key={part.key} className="run-preview-part">
              <code>{part.key}</code> — {stalenessLabel(part.staleness)}
              {part.reasons.map((reason) => (
                <div key={reason} className="work-part-reason">
                  {reason}
                </div>
              ))}
            </div>
          ))}
          {would.blocked.length > 0 && (
            <p className="run-blocked">
              {would.blocked.length} part(s) are outstanding but cannot be run:
              their text is not where the work was imported from.
            </p>
          )}
        </div>
      )}

      {loop && loop.started_at && (
        <div className="run-progress">
          <p>
            {running
              ? "Running…"
              : loop.stopped
                ? "Stopped"
                : loop.failure
                  ? `Failed: ${loop.failure}`
                  : "Finished"}
          </p>
          {loop.passes.map((report, index) => (
            <div key={index} className="run-pass">
              <strong>Pass {index + 1}</strong>: {report.scheduled.length} run,{" "}
              {report.remaining} outstanding
              {report.blocked > 0 && `, ${report.blocked} blocked`}
              {report.results.map((result) => (
                <div key={result.key} className={`run-result-${result.outcome}`}>
                  <code>{result.key}</code> {result.outcome}
                  {result.detail && ` — ${result.detail}`}
                </div>
              ))}
              {report.conflicts.map((conflict) => (
                <div key={conflict} className="run-conflict">
                  reconciler: {conflict}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
