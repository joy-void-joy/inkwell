import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchEntryPoints, fetchProfiles } from "../api/client";
import { SessionProvider } from "../context/SessionContext";
import { useSession } from "../context/session";
import { StageProgress } from "../components/StageProgress";
import { LogStream } from "../components/LogStream";
import { CostInfo, CostBreakdown } from "../components/CostPanel";
import { DocLink } from "../components/DocEmbed";
import { ActionBar } from "../components/ActionBar";
import { PromptPanel } from "../components/PromptPanel";
import { DeclaredFields } from "../components/DeclaredFields";
import {
  completedStageNames,
  declaredDefaults,
  mergedParameters,
  stageLabel,
  stageProgressIndex,
  valuesDeclaredBy,
} from "../types";
import type {
  EntryPointDescriptor,
  AgentUpdate,
  ProfileResponse,
  ServerMessage,
  SuppliedValue,
  SuppliedValues,
} from "../types";

function agentsIn(events: ServerMessage[]): AgentUpdate[] {
  const order: string[] = [];
  const byId = new Map<string, AgentUpdate>();
  for (const event of events) {
    if (event.type !== "agent") continue;
    if (!byId.has(event.agent.id)) order.push(event.agent.id);
    byId.set(event.agent.id, event.agent);
  }
  const agents: AgentUpdate[] = [];
  for (const id of order) {
    const agent = byId.get(id);
    if (agent) agents.push(agent);
  }
  return agents.sort(
    (left, right) =>
      Number(right.status === "running") - Number(left.status === "running"),
  );
}

function agentElapsed(agent: AgentUpdate, now: number): string {
  const started = new Date(agent.started_at).getTime();
  const finished = agent.finished_at ? new Date(agent.finished_at).getTime() : now;
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return "";
  const seconds = Math.max(0, Math.floor((finished - started) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

function AgentRoster({ events }: { events: ServerMessage[] }) {
  const [now, setNow] = useState(() => Date.now());
  const agents = agentsIn(events);
  const running = agents.filter((agent) => agent.status === "running").length;

  useEffect(() => {
    if (running === 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  return (
    <section className="run-agents">
      <div className="run-agents-header">
        <span>Agents</span>
        <span>{running} running · {agents.length} total</span>
      </div>
      {agents.length === 0 ? (
        <div className="run-agents-empty">No agent has opened yet.</div>
      ) : (
        <div className="run-agents-list">
          {agents.map((agent) => (
            <div className={`run-agent run-agent-${agent.status}`} key={agent.id}>
              <span className="run-agent-address">{agent.address}</span>
              {agent.model && <span className="run-agent-model">{agent.model}</span>}
              <span className="run-agent-state">
                {agent.error || agent.status} · {agentElapsed(agent, now)}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function formatSessionTitle(sessionId: string): string {
  const m = sessionId.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
  if (!m) return sessionId;
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function ResumeControls() {
  const { state, pipelineStages, resume, restart } = useSession();
  const [resumeStage, setResumeStage] = useState("");
  const [resuming, setResuming] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<ProfileResponse[]>([]);
  const [overrideProfile, setOverrideProfile] = useState("");
  const [entryPoints, setEntryPoints] = useState<EntryPointDescriptor[]>([]);
  const [declared, setDeclared] = useState<SuppliedValues>({});

  // Resume's and restart's own declarations. The stage picker and the profile
  // picker are bespoke here — they need this session's completed stages and the
  // profile list — and everything else either declaration carries is rendered
  // from its descriptor. Both actions share one panel, so the controls are the
  // union of what the two declare and each action posts only its own.
  const resumeDeclaration = entryPoints.find((e) => e.name === "resume") ?? null;
  const restartDeclaration =
    entryPoints.find((e) => e.name === "restart") ?? null;
  const sharedFields = mergedParameters([resumeDeclaration, restartDeclaration]);

  useEffect(() => {
    fetchProfiles().then(setProfiles).catch(() => {});
    fetchEntryPoints()
      .then((loaded) => {
        setEntryPoints(loaded);
        const resume = loaded.find((entry) => entry.name === "resume") ?? null;
        const restart = loaded.find((entry) => entry.name === "restart") ?? null;
        setDeclared({ ...declaredDefaults(resume), ...declaredDefaults(restart) });
      })
      .catch(() => {});
  }, []);

  const setDeclaredValue = (name: string, next: SuppliedValue) => {
    setDeclared((prev) => ({ ...prev, [name]: next }));
  };

  const hasKnownProfile = !!state.profile;
  const busy = resuming || restarting;
  const needsProfile = !hasKnownProfile && !overrideProfile;

  const currentIdx = stageProgressIndex(state.stage, pipelineStages);
  const completedStages = currentIdx > 0
    ? pipelineStages.slice(0, currentIdx)
    : [];

  // No stage picked means "the one it stopped in"; extract can't be redone on
  // resume (its sources aren't re-supplied), so it's never a restart target.
  const restartStage = resumeStage || state.stage;
  const canRestart = pipelineStages.includes(restartStage) && restartStage !== "extract";

  const reportError = (err: unknown, fallback: string) => {
    const msg = err instanceof Error ? err.message : fallback;
    if (msg.includes("no profile found") || msg.includes("specify a profile")) {
      setError("This session needs a profile to continue. Please select one.");
    } else {
      setError(msg);
    }
  };

  const handleResume = async () => {
    setResuming(true);
    setError(null);
    try {
      await resume({
        ...valuesDeclaredBy(resumeDeclaration, declared),
        from_stage: resumeStage || null,
        profile: overrideProfile || null,
      });
    } catch (err) {
      reportError(err, "Resume failed");
    } finally {
      setResuming(false);
    }
  };

  const handleRestart = async () => {
    setRestarting(true);
    setError(null);
    try {
      await restart({
        ...valuesDeclaredBy(restartDeclaration, declared),
        from_stage: restartStage,
        profile: overrideProfile || null,
      });
    } catch (err) {
      reportError(err, "Restart failed");
    } finally {
      setRestarting(false);
    }
  };

  return (
    <div className="resume-controls">
      {error && <div className="error-message" style={{ marginBottom: 8 }}>{error}</div>}
      <div className="resume-row">
        <button
          className="btn-primary"
          onClick={handleResume}
          disabled={busy || needsProfile}
        >
          {resuming ? "Resuming..." : "Resume Session"}
        </button>
        <button
          onClick={handleRestart}
          disabled={busy || needsProfile || !canRestart}
          title="Re-run the selected stage and everything after it from scratch with fresh agents"
        >
          {restarting ? "Restarting..." : "Restart Fresh"}
        </button>
        {completedStages.length > 0 && (
          <select
            value={resumeStage}
            onChange={(e) => setResumeStage(e.target.value)}
            className="resume-stage-select"
          >
            <option value="">From where it stopped</option>
            {completedStages.map((s) => (
              <option key={s} value={s}>
                {stageLabel(s)}
              </option>
            ))}
          </select>
        )}
        {profiles.length > 0 && (
          <select
            value={overrideProfile}
            onChange={(e) => setOverrideProfile(e.target.value)}
            className="resume-stage-select"
            title="Profile whose account is billed for the run"
          >
            <option value="">{hasKnownProfile ? `Keep current (${state.profile})` : "Select profile (account billed)..."}</option>
            {profiles.map((p) => (
              <option key={p.name} value={p.name}>{p.name}</option>
            ))}
          </select>
        )}
      </div>
      <DeclaredFields
        parameters={sharedFields}
        values={declared}
        onChange={setDeclaredValue}
      />
      <p className="resume-hint">
        <strong>Resume</strong> continues after the selected stage (or from where it
        stopped), picking up an interrupted agent mid-task. <strong>Restart</strong> re-runs
        the selected stage (or the one it stopped in) and everything after it from
        scratch with fresh agents, discarding their previous output.
      </p>
    </div>
  );
}

function SessionDetailInner() {
  const { state, pipelineStages, send, sendAction } = useSession();
  const isRunning = state.status === "running";
  const isEnded = state.status !== "running" && state.status !== "resuming";
  const docUrl = state.sessionState?.doc_url ?? "";
  const currentStageLabel = stageLabel(state.stage);
  const inStandby = isRunning && state.output != null;
  // A cleanly completed run has no active chip; which stages completed still
  // comes from its history, so omitted stages are never painted as successful.
  const barStage = state.status === "completed" ? "done" : state.stage;
  const completedStages = completedStageNames(state.events, state.status);

  return (
    <div className="page session-detail">
      <div className="detail-header">
        <Link to="/" className="back-link">&larr; Sessions</Link>
        <div className="detail-title-group">
          <h1>{state.title || formatSessionTitle(state.sessionId)}</h1>
          <span className="profile-badge">{state.profile}</span>
          <span className={`status-badge status-${state.status}`}>
            {state.status === "paused"
              ? `Paused after ${currentStageLabel}`
              : inStandby
                ? "Standby"
                : currentStageLabel}
          </span>
        </div>
      </div>

      <StageProgress
        currentStage={barStage}
        stages={pipelineStages}
        completedStages={completedStages}
        sections={state.sessionState?.sections ?? []}
      />

      <div className="info-bar">
        <CostInfo
          cost={state.cost}
          startedAt={state.startedAt}
          stopped={isEnded}
        />
        <DocLink docUrl={docUrl} />
      </div>

      <PromptPanel sessionId={state.sessionId} />

      {state.error && (
        <div className="error-banner">{state.error}</div>
      )}

      {isEnded && <ResumeControls />}

      <AgentRoster events={state.events} />
      <LogStream events={state.events} stopped={isEnded} />
      <CostBreakdown cost={state.cost} />

      <ActionBar
        onSend={send}
        onSync={isRunning ? () => sendAction("sync") : undefined}
        onStop={isRunning ? () => sendAction("quit") : undefined}
        placeholder={
          state.awaitingRevision
            ? "Revision instructions..."
            : "Send feedback... (Enter to send)"
        }
        disabled={state.status === "resuming"}
      />

      {state.output && (
        <div className="completion-summary">
          <h2>{state.output.title}</h2>
          {state.output.summary && <p>{state.output.summary}</p>}
          <p className="completion-stats">
            {state.output.word_count} words &middot; {state.output.review_findings_count} review findings
          </p>
          {state.output.google_doc_url && (
            <a href={state.output.google_doc_url} target="_blank" rel="noopener noreferrer" className="btn-primary">
              Open Doc
            </a>
          )}
        </div>
      )}
    </div>
  );
}

export function SessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>();

  if (!sessionId) {
    return <p>No session ID provided.</p>;
  }

  return (
    <SessionProvider sessionId={sessionId}>
      <SessionDetailInner />
    </SessionProvider>
  );
}
