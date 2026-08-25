import { memo, useState } from "react";
import { Link } from "react-router-dom";
import { stalenessLabel } from "../types";
import type { PartNode } from "../types";

// One row of the work's tree, and everywhere a part can be acted on.
//
// The action belongs beside the part rather than above the list. A selection
// carried to a button at the top of two hundred rows meant choosing a part and
// then scrolling back up to act on it, and the radio group that carried the
// selection made every row a control in one enormous form.
//
// Three verbs, one meaning each, and each offered exactly where the server
// accepts it: Run needs an idle part, Queue records one for the next loop, and
// Clear is the door out of the standings that are deliberately sticky. A note
// does not add a fourth verb — it modifies the two that take one.

export type PartVerb = "" | "run" | "queue" | "clear";

export interface PartRowProps {
  node: PartNode;
  collapsed: boolean;
  rooted: boolean;
  loopRunning: boolean;
  pending: PartVerb;
  onFold: (key: string) => void;
  onRun: (key: string, reason: string) => void;
  onQueue: (key: string, reason: string) => void;
  onClear: (key: string) => void;
}

function stateClass(node: PartNode): string {
  if (node.standing === "failed") return "part-failed";
  if (node.standing === "parked") return "part-parked";
  if (node.standing === "running") return "part-running";
  if (node.staleness === "source-gone") return "part-gone";
  return node.staleness === "fresh" ? "part-fresh" : "part-stale";
}

// Why this row offers nothing, in the words the server would refuse it with.
// A row with no buttons and no reason for having none reads as a bug.
function withheld(
  node: PartNode,
  rooted: boolean,
  loopRunning: boolean,
): string {
  if (!rooted) return "the work's directory is not there";
  if (loopRunning) return "a run over the whole work is active";
  if (node.staleness === "source-gone") return "this part has no source text";
  if (node.standing === "running") return "a run holds this part";
  return "";
}

export const PartRow = memo(function PartRow({
  node,
  collapsed,
  rooted,
  loopRunning,
  pending,
  onFold,
  onRun,
  onQueue,
  onClear,
}: PartRowProps) {
  const [revising, setRevising] = useState(false);
  const [reason, setReason] = useState("");

  // Held here rather than in the page, so typing a note re-renders one row
  // instead of every part in the work.
  const held = revising ? reason : "";
  const blocked = withheld(node, rooted, loopRunning);
  const busy = pending !== "";

  // A row mid-action keeps the buttons the action is running on. Deciding from
  // the standing alone meant clicking Run moved the row to "running", which is
  // the one standing that offers nothing — so the button reporting the click
  // disappeared at the moment it had something to report.
  const runnable = busy
    ? pending === "run" || pending === "queue"
    : !blocked && node.standing === "idle";
  const clearable = busy
    ? pending === "clear"
    : !blocked && node.standing !== "idle";

  const run = () => {
    setRevising(false);
    setReason("");
    onRun(node.key, held);
  };

  const queue = () => {
    setRevising(false);
    setReason("");
    onQueue(node.key, held);
  };

  return (
    <div
      className={`work-part ${stateClass(node)}`}
      style={{ paddingLeft: `${node.depth * 16 + 8}px` }}
    >
      <div className="work-part-line">
        {node.leaf ? (
          <span className="work-fold-gap" />
        ) : (
          <button
            className="work-fold"
            onClick={() => onFold(node.key)}
            aria-expanded={!collapsed}
            aria-label={`${collapsed ? "Expand" : "Collapse"} ${node.title}`}
          >
            {collapsed ? "▸" : "▾"}
          </button>
        )}

        <span className="work-part-key">{node.key}</span>
        <span className="work-part-title">{node.title}</span>

        {node.leaf ? (
          <span className="work-part-state">
            {node.standing !== "idle"
              ? node.standing
              : stalenessLabel(node.staleness)}
          </span>
        ) : (
          <span className="work-part-rollup">
            {node.outstanding_below === 0
              ? `${node.below} part(s), settled`
              : `${node.outstanding_below} of ${node.below} outstanding`}
          </span>
        )}

        {node.questions > 0 && (
          <span className="work-part-questions">
            {node.questions} question(s)
          </span>
        )}

        {node.session && (
          <Link
            to={`/session/${node.session}`}
            className="work-part-session"
            title="What that run actually did"
          >
            run {node.session.slice(0, 8)}
          </Link>
        )}

        {node.leaf && (
          <span className="work-part-actions">
            {runnable && (
              <>
                <button
                  className="part-action part-action-primary"
                  disabled={busy}
                  onClick={run}
                  title="Write this part now, on its own"
                >
                  {pending === "run" ? "Starting…" : "Run"}
                </button>
                <button
                  className="part-action"
                  disabled={busy}
                  onClick={queue}
                  title="Record it for the next loop over the work"
                >
                  {pending === "queue" ? "Queuing…" : "Queue"}
                </button>
                <button
                  className="part-action part-action-quiet"
                  disabled={busy}
                  aria-expanded={revising}
                  onClick={() => setRevising((open) => !open)}
                  title="Say what should change before running or queuing it"
                >
                  Note…
                </button>
              </>
            )}
            {clearable && (
              <button
                className="part-action"
                disabled={busy}
                onClick={() => onClear(node.key)}
                title="Return it to idle, so it can be run again"
              >
                {pending === "clear" ? "Clearing…" : "Clear"}
              </button>
            )}
            {node.leaf && blocked && !clearable && (
              <span className="work-part-withheld">{blocked}</span>
            )}
          </span>
        )}
      </div>

      {revising && (
        <div className="work-part-ask">
          <input
            autoFocus
            value={reason}
            placeholder={`What should change in ${node.key}?`}
            onChange={(event) => setReason(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") run();
              if (event.key === "Escape") setRevising(false);
            }}
          />
          <button
            className="part-action part-action-primary"
            disabled={busy}
            onClick={run}
          >
            Run with this
          </button>
          <button className="part-action" disabled={busy} onClick={queue}>
            Queue with this
          </button>
          <button
            className="part-action part-action-quiet"
            onClick={() => setRevising(false)}
          >
            Cancel
          </button>
        </div>
      )}

      {node.reasons.map((line) => (
        <div key={line} className="work-part-reason">
          {line}
        </div>
      ))}
    </div>
  );
},
same);

// A poll every five seconds hands back two hundred freshly built nodes, all of
// them unequal by reference and nearly all unchanged. Comparing what a row
// actually draws is what keeps that poll from re-rendering the whole tree.
function same(before: PartRowProps, after: PartRowProps): boolean {
  const one = before.node;
  const two = after.node;
  return (
    before.collapsed === after.collapsed &&
    before.rooted === after.rooted &&
    before.loopRunning === after.loopRunning &&
    before.pending === after.pending &&
    one.key === two.key &&
    one.title === two.title &&
    one.depth === two.depth &&
    one.leaf === two.leaf &&
    one.staleness === two.staleness &&
    one.standing === two.standing &&
    one.questions === two.questions &&
    one.session === two.session &&
    one.below === two.below &&
    one.outstanding_below === two.outstanding_below &&
    one.reasons.length === two.reasons.length &&
    one.reasons.every((line, index) => line === two.reasons[index])
  );
}
