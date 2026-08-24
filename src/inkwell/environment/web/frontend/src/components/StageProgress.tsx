import type { SectionInfo } from "../types";
import { stageLabel, stageProgressIndex } from "../types";

// What the write stage has to show for itself. Sections only count where the
// writer announced them, and a part of a book written as continuous prose
// announces none — so the words it has drafted stand in, against their target
// where one was declared. A counter pinned at 0/N reads as a stalled run.
function writeProgress(
  sections: SectionInfo[],
  draftedWords: number,
  targetWords: number,
): string {
  const drafted = sections.filter((s) => s.status === "drafted").length;
  if (drafted > 0) return ` ${drafted}/${sections.length}`;
  if (draftedWords <= 0) return "";
  const words = draftedWords.toLocaleString();
  return targetWords > 0
    ? ` ${words}/${targetWords.toLocaleString()} words`
    : ` ${words} words`;
}

export function StageProgress({
  currentStage,
  stages,
  completedStages = [],
  sections = [],
  draftedWords = 0,
  targetWords = 0,
}: {
  currentStage: string;
  stages: string[];
  completedStages?: string[];
  sections?: SectionInfo[];
  draftedWords?: number;
  targetWords?: number;
}) {
  const currentIdx = stageProgressIndex(currentStage, stages);
  const completed = new Set(completedStages);
  const leadIn = currentIdx === 0 && !stages.includes(currentStage);

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

        const label =
          stage === "write" && stage === currentStage
            ? `${stageLabel(stage)}${writeProgress(sections, draftedWords, targetWords)}`
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
