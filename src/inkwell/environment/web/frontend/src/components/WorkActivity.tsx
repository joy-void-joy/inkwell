import { Link } from "react-router-dom";
import type {
  PartHistory,
  RunningAgent,
  WorkActivity as Activity,
  WorkingAgent,
} from "../types";

function when(value: string): string {
  const date = new Date(value);
  return isNaN(date.getTime()) ? value : date.toLocaleString();
}

function agentState(agent: RunningAgent): string {
  if (agent.running) return "working";
  if (agent.error) return agent.error;
  return agent.summary || "finished";
}

function WorkingAgentRow({ agent }: { agent: WorkingAgent }) {
  return (
    <div className={`activity-agent ${agent.failure ? "activity-failed" : ""}`}>
      <span className="activity-agent-kind">{agent.kind}</span>{" "}
      <span>{agent.about || agent.id}</span>
      <span className="activity-agent-state">
        {agent.running
          ? "working"
          : agent.failure || agent.detail || "finished"}
      </span>
    </div>
  );
}

export function WorkActivity({ activity }: { activity: Activity | null }) {
  if (!activity) return null;
  const activeOutsideParts = activity.working.filter((agent) => agent.running);
  const recentOutsideParts = activity.working
    .filter((agent) => !agent.running)
    .slice(0, 8);
  const loop = activity.loop;

  return (
    <section className="work-activity">
      <h2>Activity</h2>
      <div className="activity-level">
        <strong>Pass</strong>{" "}
        {!loop || (!loop.running && !loop.started_at)
          ? "No pass has run in this process"
          : loop.running
            ? `running · ${loop.passes.length} completed pass(es)`
            : loop.failure
              ? `failed · ${loop.failure}`
              : loop.stopped
                ? "stopped"
                : `finished · ${loop.passes.length} pass(es)`}
      </div>

      {activity.parts.map((part) => {
        const agents = activity.agents[part.key] ?? [];
        return (
          <div className="activity-part" key={part.key}>
            <div>
              <strong>{part.key}</strong> {part.title}{" "}
              <Link to={`/session/${part.session}`}>
                run {part.session.slice(0, 8)}
              </Link>
            </div>
            {agents.length === 0 ? (
              <div className="activity-empty">No inner agent has opened yet</div>
            ) : (
              agents.map((agent) => (
                <div className="activity-inner-agent" key={agent.address}>
                  <span>{agent.kind}</span>{" "}
                  <code>{agent.address}</code>{" "}
                  <span className="activity-agent-state">
                    {agentState(agent)}
                  </span>
                </div>
              ))
            )}
          </div>
        );
      })}

      {(activeOutsideParts.length > 0 || recentOutsideParts.length > 0) && (
        <div className="activity-working">
          <strong>Work-level readers</strong>
          {activeOutsideParts.map((agent) => (
            <WorkingAgentRow key={agent.id} agent={agent} />
          ))}
          {activeOutsideParts.length === 0 && recentOutsideParts.length > 0 && (
            <span className="activity-empty"> none running</span>
          )}
          {recentOutsideParts.length > 0 && (
            <details>
              <summary>Recent completed readers</summary>
              {recentOutsideParts.map((agent) => (
                <WorkingAgentRow key={agent.id} agent={agent} />
              ))}
            </details>
          )}
        </div>
      )}

      {activity.parts.length === 0 &&
        activeOutsideParts.length === 0 &&
        (!loop || !loop.running) && (
          <div className="activity-empty">Nothing is working on this book now.</div>
        )}
    </section>
  );
}

function progress(run: PartHistory): string {
  if (run.built) return "current build";
  if (run.adopted) return "adopted, not current";
  if (run.produced) return "produced, not adopted";
  return "produced nothing";
}

export function WorkHistory({ history }: { history: PartHistory[] }) {
  return (
    <section className="work-history">
      <h2>Run history</h2>
      {history.length === 0 ? (
        <p className="activity-empty">No part of this work has run yet.</p>
      ) : (
        history.map((run) => (
          <div className="history-run" key={run.session}>
            <div className="history-run-heading">
              <Link to={`/session/${run.session}`}>
                {run.key} · {run.title || "untitled part"}
              </Link>
              <span className={run.built ? "history-current" : ""}>
                {progress(run)}
              </span>
            </div>
            <div className="history-run-detail">
              {when(run.opened_at)} · run {run.session.slice(0, 8)}
            </div>
            {run.reasons.length > 0 && (
              <div className="history-run-reasons">{run.reasons.join("; ")}</div>
            )}
          </div>
        ))
      )}
    </section>
  );
}
