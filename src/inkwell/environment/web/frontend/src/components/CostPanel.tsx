import { useState, useEffect } from "react";
import type { CostSnapshot } from "../types";
import { stageLabel } from "../types";

function formatDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.round(seconds % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function LiveTimer({ startedAt }: { startedAt: string }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const start = new Date(startedAt).getTime();
    const tick = () => setElapsed(Math.floor((Date.now() - start) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  return <span className="info-value">{formatDuration(elapsed)}</span>;
}

interface CostInfoProps {
  cost: CostSnapshot | null;
  startedAt: string | null;
  stopped: boolean;
}

export function CostInfo({ cost, startedAt, stopped }: CostInfoProps) {
  const totalTokens = cost ? cost.total_input_tokens + cost.total_output_tokens : 0;

  let timeDisplay: React.ReactNode;
  if (stopped && cost && cost.duration_s > 0) {
    timeDisplay = <span className="info-value">{formatDuration(cost.duration_s)}</span>;
  } else if (startedAt && !stopped) {
    timeDisplay = <LiveTimer startedAt={startedAt} />;
  } else {
    timeDisplay = <span className="info-value">--</span>;
  }

  return (
    <>
      <span className="info-item">
        <span className="info-label">Cost</span>
        <span className="info-value">${(cost?.total_cost_usd ?? 0).toFixed(4)}</span>
      </span>
      <span className="info-item">
        <span className="info-label">Time</span>
        {timeDisplay}
      </span>
      <span className="info-item">
        <span className="info-label">Tokens</span>
        <span className="info-value">{formatTokens(totalTokens)}</span>
      </span>
    </>
  );
}

export function CostBreakdown({ cost }: { cost: CostSnapshot | null }) {
  const [open, setOpen] = useState(false);
  const stageEntries = cost ? Object.entries(cost.stages) : [];

  if (stageEntries.length === 0) return null;

  return (
    <div className="cost-breakdown">
      <div className="cost-breakdown-header" onClick={() => setOpen(!open)}>
        <span>Cost by Stage</span>
        <span className={`cost-breakdown-toggle ${open ? "open" : ""}`}>&#9660;</span>
      </div>
      {open && (
        <div className="cost-breakdown-body">
          {stageEntries.map(([name, sc]) => (
            <div key={name} className="cost-stage-row">
              <span className="cost-stage-name">{stageLabel(name)}</span>
              <span className="cost-stage-detail">
                ${sc.cost_usd.toFixed(3)} &middot; {formatDuration(sc.duration_s)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
