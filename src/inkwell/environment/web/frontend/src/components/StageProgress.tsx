import type { SectionInfo } from "../types";
import { stageLabel, stageProgressIndex } from "../types";

export function StageProgress({
  currentStage,
  stages,
  completedStages = [],
  sections = [],
}: {
  currentStage: string;
  stages: string[];
  completedStages?: string[];
  sections?: SectionInfo[];
}) {
  const currentIdx = stageProgressIndex(currentStage, stages);
  const completed = new Set(completedStages);
  const leadIn = currentIdx === 0 && !stages.includes(currentStage);
  const drafted = sections.filter((s) => s.status === "drafted").length;

  return (
    <div className="stage-bar">
      {leadIn && (
        <span className="stage-chip active">
          <span className="stage-chip-dot" />
          {stageLabel(currentStage)}
        </span>
      )}
      {stages.map((stage, i) => {
        let cls = "stage-chip";
        if (stage === currentStage) cls += " active";
        else if (completed.has(stage)) cls += " completed";

        const activeWrite =
          stage === "write" && stage === currentStage && sections.length > 0;
        const label = activeWrite
          ? `${stageLabel(stage)} ${drafted}/${sections.length}`
          : stageLabel(stage);

        return (
          <span key={stage}>
            {(i > 0 || leadIn) && <span className="stage-sep">/</span>}
            <span className={cls}>
              <span className="stage-chip-dot" />
              {label}
            </span>
          </span>
        );
      })}
    </div>
  );
}
