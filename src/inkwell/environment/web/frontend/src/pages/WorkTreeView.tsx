import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  answerWorkQuestion,
  clearWorkPart,
  fetchWhatItReaches,
  fetchWorkQuestions,
  fetchWorkTree,
  previewWorkRun,
  requestWorkPart,
  startWorkRun,
} from "../api/client";
import { InFlight } from "../components/InFlight";
import { stalenessLabel } from "../types";
import type { PartNode, QuestionView, ReachedBy, WorkTree } from "../types";
import { RunPanel } from "./RunPanel";

// A work of two hundred parts is a tree, and a tree is what a terminal renders
// worst: `manuscript status` prints two hundred lines an author scrolls past to
// find the four that matter. Here the outstanding ones can be filtered to, a
// chapter says how many of its own parts are outstanding so the four can be
// found without reading all two hundred, the reason each is outstanding sits
// beside it, and a parked question can actually be answered.

const POLL_MS = 5000;

function stateClass(node: PartNode): string {
  if (node.standing === "failed") return "part-failed";
  if (node.standing === "parked") return "part-parked";
  if (node.standing === "running") return "part-running";
  if (node.staleness === "source-gone") return "part-gone";
  return node.staleness === "fresh" ? "part-fresh" : "part-stale";
}

export function WorkTreeView() {
  const { workId = "" } = useParams();
  const [tree, setTree] = useState<WorkTree | null>(null);
  const [questions, setQuestions] = useState<QuestionView[]>([]);
  const [error, setError] = useState("");
  const [outstandingOnly, setOutstandingOnly] = useState(false);
  const [reached, setReached] = useState<ReachedBy | null>(null);
  const [subject, setSubject] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [asking, setAsking] = useState("");
  const [reason, setReason] = useState("");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  // A counter rather than a hoisted loader, so the fetch lives entirely inside
  // the effect and nothing sets state synchronously in the effect body. An
  // action bumps it; the effect is what reads.
  const [reloads, setReloads] = useState(0);
  const reload = useCallback(() => setReloads((held) => held + 1), []);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const [loaded, waiting] = await Promise.all([
          fetchWorkTree(workId),
          fetchWorkQuestions(workId),
        ]);
        if (!live) return;
        setTree(loaded);
        setQuestions(waiting);
        setError("");
      } catch (failure) {
        if (live) setError(String(failure));
      }
    };
    load();
    const interval = setInterval(load, POLL_MS);
    return () => {
      live = false;
      clearInterval(interval);
    };
  }, [workId, reloads]);

  // Recorded rather than run, which is what an author revising several parts
  // wants: say what each needs, then take one loop over the work instead of a
  // pass per part. Reported when it fails, because a row that silently stayed
  // as it was reads exactly like one nothing was asked of.
  const askFor = async (key: string) => {
    try {
      await requestWorkPart(workId, key, reason);
      setAsking("");
      setReason("");
      reload();
    } catch (failure) {
      setError(String(failure));
    }
  };

  // One part, one pass. A loop narrowed to a single part would only ever pick
  // that part up again, and a rewrite leaves it fresh, so further passes are
  // reserved for the propagation this deliberately does not chase: revising one
  // subsection puts the parts leaning on it out of date and leaves them there,
  // which is what asking for one subsection meant.
  //
  // Asked before started, the way the run panel asks before its own button: a
  // part the sweep will not pick up is reported here rather than starting a
  // pass that runs nothing and reads as a finished book.
  const runNow = async (node: PartNode, reason: string) => {
    try {
      // A pass picks up what is out of date, so a part that is already up to
      // date has to be asked for before it can be run at all — and "run it
      // again" is a thing an author wants without wanting anything in
      // particular changed, so nothing here is a reason. A part that is
      // outstanding already carries the reason it is outstanding, which is
      // worth more on its row than an empty one written over the top.
      if (reason || node.staleness === "fresh")
        await requestWorkPart(workId, node.key, reason);
      const would = await previewWorkRun(workId, 0, [node.key]);
      const [skipped] = would.passed_over;
      if (skipped) {
        setError(`${skipped.key} is not being run: ${skipped.reason}`);
        return;
      }
      await startWorkRun(workId, { passes: 1, parts: [node.key] });
      setAsking("");
      setReason("");
      reload();
    } catch (failure) {
      setError(String(failure));
    }
  };

  const clear = async (key: string) => {
    try {
      await clearWorkPart(workId, key);
      reload();
    } catch (failure) {
      setError(String(failure));
    }
  };

  const answer = async (question: string) => {
    const value = answers[question]?.trim();
    if (!value) return;
    await answerWorkQuestion(workId, question, value);
    setAnswers((held) => ({ ...held, [question]: "" }));
    reload();
  };

  const lookUp = async () => {
    if (!subject.trim()) return;
    setReached(await fetchWhatItReaches(workId, subject.trim()));
  };

  if (error && !tree) return <div className="page">{error}</div>;
  if (!tree) return <div className="page">Loading...</div>;

  // A row is hidden when any part above it is collapsed, so folding a chapter
  // folds everything under it without the tree having to be nested.
  const folded = (node: PartNode) =>
    tree.nodes.some(
      (other) =>
        collapsed[other.key] &&
        other.key !== node.key &&
        node.key.startsWith(`${other.key}/`),
    );

  const shown = tree.nodes.filter(
    (node) =>
      !folded(node) &&
      (!outstandingOnly ||
        node.staleness !== "fresh" ||
        node.outstanding_below > 0),
  );

  return (
    <div className="page work-tree">
      <div className="page-header">
        <h1>{tree.title}</h1>
        <Link to="/works" className="back-link">
          Works
        </Link>
      </div>

      {!tree.rooted && (
        <p className="run-blocked">
          Imported from <code>{tree.root}</code>, which is not there. Every part
          reads as having lost its text because of that one directory — re-import
          the work to pick it up from where it lives now.
        </p>
      )}

      {error && <p className="run-error">{error}</p>}

      <p>
        {tree.settled
          ? "Settled — nothing outstanding"
          : `${tree.outstanding} part(s) outstanding`}
        {tree.blocked > 0 && ` · ${tree.blocked} blocked`}
        {" · "}
        <label>
          <input
            type="checkbox"
            checked={outstandingOnly}
            onChange={(event) => setOutstandingOnly(event.target.checked)}
          />{" "}
          show only what is outstanding
        </label>
      </p>

      <RunPanel
        workId={workId}
        loop={tree.loop}
        rooted={tree.rooted}
        targetFormat={tree.target_format}
        onChanged={reload}
      />

      <InFlight parts={tree.in_flight} onChanged={reload} />

      {questions.length > 0 && (
        <section>
          <h2>Waiting on an answer</h2>
          {questions.map((question) => (
            <div key={question.id} className="work-question">
              <p>
                <strong>{question.asker}</strong> asks{" "}
                {question.addressed_to || tree.id}: {question.prompt}
              </p>
              <input
                value={answers[question.id] ?? ""}
                placeholder="Your answer"
                onChange={(event) =>
                  setAnswers((held) => ({
                    ...held,
                    [question.id]: event.target.value,
                  }))
                }
              />
              <button onClick={() => answer(question.id)}>Answer</button>
            </div>
          ))}
        </section>
      )}

      <section>
        <h2>What a change would reach</h2>
        <input
          value={subject}
          placeholder="A term the work declares"
          onChange={(event) => setSubject(event.target.value)}
        />
        <button onClick={lookUp}>Look up</button>
        {reached && (
          <p>
            {reached.parts.length} part(s) lean on {reached.kind}{" "}
            <code>{reached.subject}</code>
            {reached.parts.length > 0 && `: ${reached.parts.join(", ")}`}
          </p>
        )}
      </section>

      <section>
        <h2>Parts</h2>
        {shown.map((node) => (
          <div
            key={node.key}
            className={`work-part ${stateClass(node)}`}
            style={{ paddingLeft: `${node.depth * 16}px` }}
          >
            {!node.leaf && (
              <button
                className="work-fold"
                onClick={() =>
                  setCollapsed((held) => ({
                    ...held,
                    [node.key]: !held[node.key],
                  }))
                }
              >
                {collapsed[node.key] ? "▸" : "▾"}
              </button>
            )}
            <span className="work-part-key">{node.key}</span>{" "}
            <span className="work-part-title">{node.title}</span>{" "}
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
                {" "}
                · {node.questions} question(s)
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
            {node.leaf &&
              node.staleness !== "source-gone" &&
              node.standing === "idle" && (
                <button
                  onClick={() => runNow(node, "")}
                  title="Take this part through the pipeline now, and nothing else"
                >
                  {node.staleness === "fresh" ? "Run it again" : "Run this part"}
                </button>
              )}
            {/* Not offered while a run holds the part: what asking would write
                over is the lease, and the run named in it is the only way back
                to what is being done to the part. */}
            {node.leaf && node.standing !== "running" && asking !== node.key && (
              <button
                onClick={() => setAsking(node.key)}
                title="Run it with something particular to change"
              >
                Revise…
              </button>
            )}
            {node.leaf && node.standing !== "idle" && (
              <button onClick={() => clear(node.key)} title="Return it to idle">
                Clear
              </button>
            )}
            {asking === node.key && (
              <span className="work-part-ask">
                <input
                  autoFocus
                  value={reason}
                  placeholder={`What should change in ${node.key}? Empty runs it as it stands`}
                  onChange={(event) => setReason(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") runNow(node, reason);
                    if (event.key === "Escape") setAsking("");
                  }}
                />
                <button onClick={() => runNow(node, reason)}>
                  Revise it now
                </button>
                <button
                  onClick={() => askFor(node.key)}
                  title="Record it and leave it for the next loop over the work"
                >
                  Queue it
                </button>
                <button onClick={() => setAsking("")}>Cancel</button>
              </span>
            )}
            {node.reasons.map((reason) => (
              <div key={reason} className="work-part-reason">
                {reason}
              </div>
            ))}
          </div>
        ))}
      </section>
    </div>
  );
}
