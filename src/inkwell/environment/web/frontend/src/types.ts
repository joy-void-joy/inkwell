export interface FormatOption {
  key: string;
  label: string;
  accepts_description: boolean;
}

export interface ModelOptions {
  stages: string[];
  suggested_models: string[];
  writer_modes: string[];
  default_model: string;
  default_writer_mode: string;
  default_stage_models: Record<string, string>;
}

export interface ModelConfig {
  model?: string;
  stage_models?: Record<string, string>;
  writer_mode?: string;
}

export type SessionStatus = "running" | "completed" | "failed" | "cancelled" | "interrupted" | "resuming";

export interface StageCostSummary {
  cost_usd: number;
  duration_s: number;
  input_tokens: number;
  output_tokens: number;
  calls: number;
}

export interface CostSnapshot {
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  duration_s: number;
  stages: Record<string, StageCostSummary>;
}

export interface SectionInfo {
  title: string;
  tab_id: string;
  status: string;
}

export interface SessionStateSnapshot {
  doc_id: string;
  doc_url: string;
  title: string;
  stage: string;
  sections: SectionInfo[];
  pending_questions: string[];
}

export interface SessionSummary {
  session_id: string;
  title: string;
  status: SessionStatus;
  stage: string;
  cost_usd: number;
  duration_s: number;
  doc_url: string;
  created_at: string;
  profile: string | null;
  checkpoints: string[];
}

export interface SessionDetail {
  session_id: string;
  title: string;
  status: SessionStatus;
  state: SessionStateSnapshot;
  cost: CostSnapshot;
  created_at: string;
  profile: string | null;
  events: ServerMessage[];
  output?: CompletionOutput | null;
  checkpoints: string[];
}

export interface CompletionOutput {
  title: string;
  word_count: number;
  summary: string;
  google_doc_url: string;
  review_findings_count: number;
}

export interface GeneratingPrompt {
  raw_sources: string[];
  author_instructions: string;
  author_deliverables: string[];
}

// WebSocket message types
export type ServerMessage =
  | { type: "stage"; stage: string; description: string; timestamp: string }
  | { type: "progress"; message: string; timestamp: string }
  | { type: "message"; source: string; message: string; timestamp: string }
  | { type: "block"; block_type: string; content: string; prefix: string; timestamp: string }
  | { type: "complete"; output: CompletionOutput; timestamp: string }
  | { type: "cost_update"; cost: CostSnapshot; timestamp: string }
  | { type: "state_update"; state: SessionStateSnapshot; timestamp: string }
  | { type: "collect_revision"; state: SessionStateSnapshot; timestamp: string }
  | { type: "error"; message: string; timestamp: string }
  | { type: "session_ended"; status: SessionStatus; timestamp: string };

// --- Profiles ---

export interface IntegrationStatus {
  name: string;
  configured: boolean;
  detail: string;
}

export interface ProfileResponse {
  name: string;
  integrations: IntegrationStatus[];
}

export interface GoogleStatus {
  has_credentials: boolean;
  has_token: boolean;
  detail: string;
}

export interface ServerCapabilities {
  has_display: boolean;
  is_local: boolean;
}

// The ordered stage backbone is fetched from GET /api/pipeline-stages so it
// always mirrors the pipeline. STAGE_LABELS only supplies display text; any
// stage without an entry falls back to a title-cased key, so a backbone stage
// added on the server still renders rather than blanking the bar.
export const STAGE_LABELS: Record<string, string> = {
  starting: "Starting",
  preprocess: "Classify",
  extract: "Extract",
  voice: "Voice",
  plan: "Plan",
  research: "Research",
  assumptions: "Questions",
  refine: "Refine",
  write: "Write",
  merge: "Merge",
  review: "Review",
  resolve: "Resolve",
  rewrite: "Rewrite",
  format: "Format",
  sync: "Standby",
  revise: "Revise",
  done: "Complete",
  complete: "Complete",
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.charAt(0).toUpperCase() + stage.slice(1);
}

// Stages that sit past the linear backbone: map them onto a full bar so a
// finished or standby session reads as all-complete instead of going blank.
const TERMINAL_STAGES = new Set(["done", "complete", "sync", "revise"]);

// Index of the active stage within `stages`; everything before it is complete.
// Off-backbone stages resolve to an end (terminal) or the start (lead-ins like
// preprocess/starting, and anything unrecognized) so the bar never blanks.
export function stageProgressIndex(stage: string, stages: string[]): number {
  if (stages.length === 0) return -1;
  const idx = stages.indexOf(stage);
  if (idx !== -1) return idx;
  return TERMINAL_STAGES.has(stage) ? stages.length : 0;
}
