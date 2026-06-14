import type { SectionInfo } from "../types";
import { stageLabel, stageProgressIndex } from "../types";

export function StageProgress({
  currentStage,
  stages,
  sections = [],
}: {
  currentStage: string;
  stages: string[];
  sections?: SectionInfo[];
}) {
  const currentIdx = stageProgressIndex(currentStage, stages);
  const drafted = sections.filter((s) => s.status === "drafted").length;

  return (
    <div className="stage-bar">
      {stages.map((stage, i) => {
        let cls = "stage-chip";
        if (i < currentIdx) cls += " completed";
        else if (i === currentIdx) cls += " active";

        const activeWrite =
          stage === "write" && i === currentIdx && sections.length > 0;
        const label = activeWrite
          ? `${stageLabel(stage)} ${drafted}/${sections.length}`
          : stageLabel(stage);

        return (
          <span key={stage}>
            {i > 0 && <span className="stage-sep">/</span>}
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
