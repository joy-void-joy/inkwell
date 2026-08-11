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

// --- Entry points ---
//
// Every way a session starts, and the parameters it takes, are fetched from
// GET /api/entry-points, which is rendered off the one declaration the typer
// commands and the API request models are compiled from too. Nothing below
// names a parameter: the form builds its controls from what arrives, so a
// parameter added to the declaration shows up here without an edit.

export type ParameterWidget =
  | "text"
  | "textarea"
  | "lines"
  | "flag"
  | "select"
  | "map";

export type SuppliedValue =
  | string
  | boolean
  | string[]
  | Record<string, string>
  | null;

export type SuppliedValues = Record<string, SuppliedValue>;

export interface ParameterDescriptor {
  name: string;
  label: string;
  help: string;
  widget: ParameterWidget;
  default: SuppliedValue;
  required: boolean;
  options_endpoint: string;
  // False when the page carries bespoke UI for this parameter — a source
  // picker, a format description, a model grid — that no descriptor describes.
  rendered: boolean;
}

export interface EntryPointDescriptor {
  name: string;
  summary: string;
  detail: string;
  parameters: ParameterDescriptor[];
}

export function declaredParameter(
  entryPoint: EntryPointDescriptor | null,
  name: string,
): ParameterDescriptor | undefined {
  return entryPoint?.parameters.find((p) => p.name === name);
}

export function carries(
  entryPoint: EntryPointDescriptor | null,
  name: string,
): boolean {
  return declaredParameter(entryPoint, name) !== undefined;
}

export function genericParameters(
  entryPoint: EntryPointDescriptor | null,
): ParameterDescriptor[] {
  return (entryPoint?.parameters ?? []).filter((p) => p.rendered);
}

export function declaredDefaults(
  entryPoint: EntryPointDescriptor | null,
): SuppliedValues {
  return Object.fromEntries(
    genericParameters(entryPoint).map((p) => [p.name, p.default]),
  );
}

export type SessionStatus = "running" | "completed" | "failed" | "cancelled" | "interrupted" | "resuming" | "paused";

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
  // Docs/Drive APIs switched off on the client's Cloud project: empty when all
  // enabled, hostnames when off, null when not probed. A token can exist while
  // these are off — writing a Doc then 403s and re-authorizing won't fix it.
  disabled_apis: string[] | null;
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
