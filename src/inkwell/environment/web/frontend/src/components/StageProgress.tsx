import { PIPELINE_STAGES, STAGE_LABELS } from "../types";

export function StageProgress({ currentStage }: { currentStage: string }) {
  const currentIdx = currentStage === "done"
    ? PIPELINE_STAGES.length
    : PIPELINE_STAGES.indexOf(currentStage as (typeof PIPELINE_STAGES)[number]);

  return (
    <div className="stage-bar">
      {PIPELINE_STAGES.map((stage, i) => {
        let cls = "stage-chip";
        if (i < currentIdx) cls += " completed";
        else if (i === currentIdx) cls += " active";

        return (
          <span key={stage}>
            {i > 0 && <span className="stage-sep">/</span>}
            <span className={cls}>
              <span className="stage-chip-dot" />
              {STAGE_LABELS[stage]}
            </span>
          </span>
        );
      })}
    </div>
  );
}
