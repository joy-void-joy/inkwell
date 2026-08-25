import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  answerWorkQuestion,
  clearWorkPart,
  fetchWorkActivity,
  fetchWorkHistory,
  fetchWhatItReaches,
  fetchWorkQuestions,
  fetchWorkTree,
  requestWorkPart,
  startWorkPart,
} from "../api/client";
import { InFlight } from "../components/InFlight";
import { PartRow } from "../components/PartRow";
import type { PartVerb } from "../components/PartRow";
import { WorkActivity, WorkHistory } from "../components/WorkActivity";
import type {
  PartHistory,
  PartNode,
  QuestionView,
  ReachedBy,
  WorkActivity as Activity,
  WorkTree,
} from "../types";
import { RunPanel } from "./RunPanel";

// A work of two hundred parts is a tree, and a tree is what a terminal renders
// worst: `manuscript status` prints two hundred lines an author scrolls past to
// find the four that matter. Here the outstanding ones can be filtered to, a
// chapter says how many of its own parts are outstanding so the four can be
// found without reading all two hundred, the reason each is outstanding sits
// beside it, and a parked question can actually be answered.
//
// The tree comes before the run panel's neighbours for the same reason: it is
// what the page is for. Activity, history, and the reachability lookup are read
// after the author has found their part, so they sit below it.

const POLL_MS = 5000;

export function WorkTreeView() {
  const { workId = "" } = useParams();
  const [tree, setTree] = useState<WorkTree | null>(null);
  const [questions, setQuestions] = useState<QuestionView[]>([]);
  const [activity, setActivity] = useState<Activity | null>(null);
  const [history, setHistory] = useState<PartHistory[]>([]);
  // Two, because one was cleared by whichever poll came next: an action that
  // was refused reported it for however many milliseconds were left before the
  // tree came back, and then the page looked like nothing had gone wrong.
  const [error, setError] = useState("");
  const [refused, setRefused] = useState("");
  const [outstandingOnly, setOutstandingOnly] = useState(false);
  const [reached, setReached] = useState<ReachedBy | null>(null);
  const [subject, setSubject] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [done, setDone] = useState("");

  // Which verb is in flight for which part. Per part rather than one flag for
  // the page: acting on one row used to disable every button in the work, so a
  // click on a two-hundred-part tree froze all of it until the server answered.
  const [pending, setPending] = useState<Record<string, PartVerb>>({});

  // The poll reads this. A row whose action is still in flight keeps what the
  // click said rather than being overwritten by a tree fetched before the
  // server had recorded it — which is what made a button look like it did
  // nothing, and the honest response to that is to click it again.
  const inFlight = useRef<Record<string, PartVerb>>({});
  const claim = useCallback((key: string, verb: PartVerb) => {
    inFlight.current = { ...inFlight.current, [key]: verb };
    setPending((held) => ({ ...held, [key]: verb }));
  }, []);
  const release = useCallback((key: string) => {
    inFlight.current = without(inFlight.current, key);
    setPending((held) => without(held, key));
  }, []);

  // A counter rather than a hoisted loader, so the fetch lives entirely inside
  // the effect and nothing sets state synchronously in the effect body. An
  // action bumps it; the effect is what reads.
  const [reloads, setReloads] = useState(0);
  const reload = useCallback(() => setReloads((held) => held + 1), []);

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const [loaded, waiting, happening, runs] = await Promise.all([
          fetchWorkTree(workId),
          fetchWorkQuestions(workId),
          fetchWorkActivity(workId),
          fetchWorkHistory(workId),
        ]);
        if (!live) return;
        setTree((held) => settled(held, loaded, inFlight.current));
        setQuestions(waiting);
        setActivity(happening);
        setHistory(runs);
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

  // What a part's row says, before the server has been asked or has answered.
  // Every action here shows its effect on the row at once and reconciles with
  // whatever comes back, so the page moves when it is clicked rather than a
  // round trip and a four-endpoint refetch later.
  const patch = useCallback((key: string, node: Partial<PartNode>) => {
    setTree((held) =>
      held
        ? {
            ...held,
            nodes: held.nodes.map((one) =>
              one.key === key ? { ...one, ...node } : one,
            ),
          }
        : held,
    );
  }, []);

  // `refresh` is for an action whose effect is wider than the row it was
  // clicked on. Request and clear each answer with the part as it now stands,
  // so taking that answer is the whole update — refetching the tree, the
  // questions, the activity and the history to learn what the response already
  // said is the wait this page was full of.
  const acted = useCallback(
    async (
      key: string,
      verb: PartVerb,
      expected: Partial<PartNode>,
      call: () => Promise<Partial<PartNode> | null>,
      refresh = false,
    ) => {
      const before = tree?.nodes.find((one) => one.key === key);
      claim(key, verb);
      setDone("");
      setRefused("");
      patch(key, expected);
      try {
        const answered = await call();
        if (answered) patch(key, answered);
        if (refresh) reload();
      } catch (failure) {
        setRefused(String(failure));
        if (before) patch(key, before);
      } finally {
        release(key);
      }
    },
    [claim, patch, release, reload, tree],
  );

  // Recorded rather than run, which is what an author revising several parts
  // wants: say what each needs, then take one loop over the work instead of a
  // pass per part.
  const queue = useCallback(
    (key: string, reason: string) =>
      acted(
        key,
        "queue",
        { standing: "requested", reasons: reason ? [reason] : [] },
        async () => {
          const node = await requestWorkPart(workId, key, reason);
          setDone(`${key} is queued — the next loop over the work picks it up`);
          return node;
        },
      ),
    [acted, workId],
  );

  // One part, one pass. A loop narrowed to a single part would only ever pick
  // that part up again, and a rewrite leaves it fresh, so further passes are
  // reserved for the propagation this deliberately does not chase: revising one
  // subsection puts the parts leaning on it out of date and leaves them there,
  // which is what asking for one subsection meant.
  //
  // The server claims the work, prepares the part, and starts the pass as one
  // operation. A second surface cannot win between those steps and leave this
  // click looking queued when it never started.
  const run = useCallback(
    (key: string, reason: string) =>
      acted(key, "run", { standing: "running" }, async () => {
        const started = await startWorkPart(workId, key, reason);
        setTree((held) => (held ? { ...held, loop: started } : held));
        setDone(`Started a run of ${key} — it appears under Being written now`);
        return null;
      }, true),
    [acted, workId],
  );

  const clear = useCallback(
    (key: string) =>
      acted(key, "clear", { standing: "idle", reasons: [] }, async () => {
        const node = await clearWorkPart(workId, key);
        setDone(`${key} is back to idle`);
        return node;
      }),
    [acted, workId],
  );

  const fold = useCallback((key: string) => {
    setCollapsed((held) => ({ ...held, [key]: !held[key] }));
  }, []);

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

  // A row is hidden when any part above it is collapsed, so folding a chapter
  // folds everything under it without the tree having to be nested. Built from
  // the shut chapters rather than asked of every node against every other,
  // which on two hundred parts was forty thousand comparisons a keystroke.
  const shown = useMemo(() => {
    if (!tree) return [];
    const shut = tree.nodes
      .filter((one) => collapsed[one.key])
      .map((one) => `${one.key}/`);
    return tree.nodes.filter(
      (node) =>
        !shut.some((prefix) => node.key.startsWith(prefix)) &&
        (!outstandingOnly ||
          node.staleness !== "fresh" ||
          node.outstanding_below > 0),
    );
  }, [tree, collapsed, outstandingOnly]);

  if (error && !tree) return <div className="page">{error}</div>;
  if (!tree) return <div className="page">Loading...</div>;

  const loopRunning = tree.loop?.running ?? false;

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
      {refused && <p className="run-error">{refused}</p>}
      {done && <p className="work-recorded">{done}</p>}

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

      <section className="work-parts">
        <div className="work-parts-header">
          <h2>Parts</h2>
          <span className="work-parts-count">
            {tree.settled
              ? "settled — nothing outstanding"
              : `${tree.outstanding} outstanding`}
            {tree.blocked > 0 && ` · ${tree.blocked} blocked`}
          </span>
          <label className="work-parts-filter">
            <input
              type="checkbox"
              checked={outstandingOnly}
              onChange={(event) => setOutstandingOnly(event.target.checked)}
            />{" "}
            only what is outstanding
          </label>
        </div>

        {loopRunning && (
          <p className="work-parts-note">
            A run over the whole work is active — a part cannot be run on its own
            until it finishes.
          </p>
        )}

        {shown.map((node) => (
          <PartRow
            key={node.key}
            node={node}
            collapsed={Boolean(collapsed[node.key])}
            rooted={tree.rooted}
            loopRunning={loopRunning}
            pending={pending[node.key] ?? ""}
            onFold={fold}
            onRun={run}
            onQueue={queue}
            onClear={clear}
          />
        ))}
      </section>

      <WorkActivity activity={activity} />

      <WorkHistory history={history} />

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
    </div>
  );
}

// One key gone from a record, without naming the value being discarded.
function without(
  held: Record<string, PartVerb>,
  key: string,
): Record<string, PartVerb> {
  return Object.fromEntries(
    Object.entries(held).filter(([one]) => one !== key),
  );
}

// The freshly fetched work, with any row still waiting on its own action left
// as the click left it. Everything else — the loop, the counts, every part
// nobody is acting on — comes from the server.
function settled(
  held: WorkTree | null,
  loaded: WorkTree,
  waiting: Record<string, PartVerb>,
): WorkTree {
  if (!held || Object.keys(waiting).length === 0) return loaded;
  return {
    ...loaded,
    nodes: loaded.nodes.map((node) => {
      const kept = waiting[node.key]
        ? held.nodes.find((one) => one.key === node.key)
        : undefined;
      return kept ?? node;
    }),
  };
}
