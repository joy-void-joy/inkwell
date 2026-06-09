import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchProfiles } from "../api/client";
import { SessionProvider, useSession } from "../context/SessionContext";
import { StageProgress } from "../components/StageProgress";
import { LogStream } from "../components/LogStream";
import { CostInfo, CostBreakdown } from "../components/CostPanel";
import { DocLink } from "../components/DocEmbed";
import { ActionBar } from "../components/ActionBar";
import { PIPELINE_STAGES, STAGE_LABELS } from "../types";
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
  const { state, resume } = useSession();
  const [resumeStage, setResumeStage] = useState("");
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<ProfileResponse[]>([]);
  const [overrideProfile, setOverrideProfile] = useState("");
  const [needsProfile, setNeedsProfile] = useState(false);

  useEffect(() => {
    fetchProfiles().then(setProfiles).catch(() => {});
  }, []);

  const hasKnownProfile = !!state.profile;

  const currentIdx = PIPELINE_STAGES.indexOf(
    state.stage as (typeof PIPELINE_STAGES)[number],
  );
  const completedStages = currentIdx > 0
    ? PIPELINE_STAGES.slice(0, currentIdx)
    : [];

  const handleResume = async () => {
    setResuming(true);
    setError(null);
    try {
      const profileArg = overrideProfile || undefined;
      await resume(resumeStage || undefined, profileArg);
      setNeedsProfile(false);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Resume failed";
      if (msg.includes("no profile found") || msg.includes("specify a profile")) {
        setNeedsProfile(true);
        setError("This session needs a profile to resume. Please select one.");
      } else {
        setError(msg);
      }
    } finally {
      setResuming(false);
    }
  };

  return (
    <div className="resume-controls">
      {error && <div className="error-message" style={{ marginBottom: 8 }}>{error}</div>}
      <div className="resume-row">
        <button
          className="btn-primary"
          onClick={handleResume}
          disabled={resuming || (needsProfile && !overrideProfile)}
        >
          {resuming ? "Resuming..." : "Resume Session"}
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
                Redo from {STAGE_LABELS[s]}
              </option>
            ))}
          </select>
        )}
        {(needsProfile || !hasKnownProfile) && profiles.length > 0 && (
          <select
            value={overrideProfile}
            onChange={(e) => setOverrideProfile(e.target.value)}
            className="resume-stage-select"
          >
            <option value="">Select profile...</option>
            {profiles.map((p) => (
              <option key={p.name} value={p.name}>{p.name}</option>
            ))}
          </select>
        )}
      </div>
    </div>
  );
}

function SessionDetailInner() {
  const { state, send, sendAction } = useSession();
  const isRunning = state.status === "running";
  const isEnded = state.status !== "running" && state.status !== "resuming";
  const docUrl = state.sessionState?.doc_url ?? "";
  const stageLabel = STAGE_LABELS[state.stage] ?? state.stage;
  const inStandby = isRunning && state.output != null;

  return (
    <div className="page session-detail">
      <div className="detail-header">
        <Link to="/" className="back-link">&larr; Sessions</Link>
        <div className="detail-title-group">
          <h1>{state.title || formatSessionTitle(state.sessionId)}</h1>
          <span className="profile-badge">{state.profile}</span>
          <span className={`status-badge status-${state.status}`}>
            {inStandby ? "Standby" : stageLabel}
          </span>
        </div>
      </div>

      <StageProgress currentStage={state.stage} />

      <div className="info-bar">
        <CostInfo
          cost={state.cost}
          startedAt={state.startedAt}
          stopped={isEnded}
        />
        <DocLink docUrl={docUrl} />
      </div>

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
