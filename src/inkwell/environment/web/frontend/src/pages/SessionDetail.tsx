import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchProfiles } from "../api/client";
import { SessionProvider, useSession } from "../context/SessionContext";
import { StageProgress } from "../components/StageProgress";
import { LogStream } from "../components/LogStream";
import { CostInfo, CostBreakdown } from "../components/CostPanel";
import { DocLink } from "../components/DocEmbed";
import { ActionBar } from "../components/ActionBar";
import { PromptPanel } from "../components/PromptPanel";
import { stageLabel, stageProgressIndex } from "../types";
import type { ProfileResponse } from "../types";

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

  useEffect(() => {
    fetchProfiles().then(setProfiles).catch(() => {});
  }, []);

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
      await resume(resumeStage || undefined, overrideProfile || undefined);
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
      await restart(restartStage, overrideProfile || undefined);
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
  // A cleanly completed run reads as all-complete even if its last stage event
  // was a backbone stage rather than the standby poll.
  const barStage = state.status === "completed" ? "done" : state.stage;

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
