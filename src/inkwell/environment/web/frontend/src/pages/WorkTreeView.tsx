import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  answerWorkQuestion,
  fetchWhatItReaches,
  fetchWorkQuestions,
  fetchWorkTree,
  requestWorkPart,
} from "../api/client";
import { stalenessLabel } from "../types";
import type { PartNode, QuestionView, ReachedBy, WorkTree } from "../types";

// A work of two hundred parts is a tree, and a tree is what a terminal renders
// worst: `manuscript status` prints two hundred lines an author scrolls past to
// find the four that matter. Here the outstanding ones can be filtered to, the
// reason each is outstanding sits beside it, and a parked question can actually
// be answered — which is the one thing the command line cannot do at all, and
// the reason the loop can be left running unattended.

const POLL_MS = 5000;

function stateClass(node: PartNode): string {
  if (node.standing === "failed") return "part-failed";
  if (node.standing === "parked") return "part-parked";
  if (node.standing === "running") return "part-running";
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

  const askFor = async (key: string) => {
    const reason = window.prompt(`What should change in ${key}?`);
    if (reason === null) return;
    await requestWorkPart(workId, key, reason);
    reload();
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

  if (error) return <div className="page">{error}</div>;
  if (!tree) return <div className="page">Loading...</div>;

  const shown = outstandingOnly
    ? tree.nodes.filter((node) => node.staleness !== "fresh")
    : tree.nodes;

  return (
    <div className="page work-tree">
      <div className="page-header">
        <h1>{tree.title}</h1>
        <Link to="/" className="back-link">
          Sessions
        </Link>
      </div>

      <p>
        {tree.settled
          ? "Settled — nothing outstanding"
          : `${tree.outstanding} part(s) outstanding`}
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
            <span className="work-part-key">{node.key}</span>{" "}
            <span className="work-part-title">{node.title}</span>{" "}
            <span className="work-part-state">
              {node.standing !== "idle"
                ? node.standing
                : stalenessLabel(node.staleness)}
            </span>
            {node.questions > 0 && (
              <span className="work-part-questions">
                {" "}
                · {node.questions} question(s)
              </span>
            )}
            {node.leaf && (
              <button onClick={() => askFor(node.key)}>Revise</button>
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
