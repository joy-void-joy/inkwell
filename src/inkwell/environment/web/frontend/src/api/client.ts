import type {
  EntryPointDescriptor,
  FormatOption,
  GeneratingPrompt,
  GoogleStatus,
  ModelOptions,
  ProfileResponse,
  ServerCapabilities,
  PartNode,
  QuestionView,
  ReachedBy,
  SessionDetail,
  SessionSummary,
  SuppliedValues,
  WorkImport,
  WorkLoopStatus,
  WorkSummary,
  WorkTree,
  WouldRun,
} from "../types";

const BASE = `${import.meta.env.BASE_URL}api`;

export async function fetchFormats(): Promise<FormatOption[]> {
  const res = await fetch(`${BASE}/formats`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchModelOptions(): Promise<ModelOptions> {
  const res = await fetch(`${BASE}/model-options`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchPipelineStages(): Promise<string[]> {
  const res = await fetch(`${BASE}/pipeline-stages`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchEntryPoints(): Promise<EntryPointDescriptor[]> {
  const res = await fetch(`${BASE}/entry-points`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// Option list for a parameter whose descriptor names an endpoint to read it
// from — a stage picker reads the pipeline's own checkpoint stages this way.
export async function fetchOptions(endpoint: string): Promise<string[]> {
  const res = await fetch(`${BASE}/${endpoint}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// Start one of the fresh entry points. The body is the values keyed by their
// declared names, so nothing here names a parameter.
export async function startSession(
  entryPoint: string,
  values: SuppliedValues,
): Promise<{ session_id: string; status: string }> {
  const res = await fetch(`${BASE}/sessions/${entryPoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function listSessions(): Promise<SessionSummary[]> {
  const res = await fetch(`${BASE}/sessions`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getSession(id: string): Promise<SessionDetail> {
  const res = await fetch(`${BASE}/sessions/${id}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getSessionPrompt(id: string): Promise<GeneratingPrompt> {
  const res = await fetch(`${BASE}/sessions/${id}/prompt`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function resumeSession(
  id: string,
  values: SuppliedValues = {},
): Promise<{ session_id: string; status: string }> {
  const res = await fetch(`${BASE}/sessions/${id}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function restartSession(
  id: string,
  values: SuppliedValues,
): Promise<{ session_id: string; status: string }> {
  const res = await fetch(`${BASE}/sessions/${id}/restart`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export interface UploadResult {
  path: string;
  filename: string;
  size: number;
}

export async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/sessions/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function uploadText(
  filename: string,
  content: string,
  replacePath?: string,
): Promise<UploadResult> {
  const res = await fetch(`${BASE}/sessions/upload-text`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filename,
      content,
      replace_path: replacePath ?? null,
    }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function sendAction(
  id: string,
  action: string,
  text?: string,
): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${id}/action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, text }),
  });
  if (!res.ok) throw new Error(await res.text());
}

// --- Server capabilities ---

export async function fetchCapabilities(): Promise<ServerCapabilities> {
  const res = await fetch(`${BASE}/profiles/capabilities`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// --- Profiles ---

export async function fetchProfiles(): Promise<ProfileResponse[]> {
  const res = await fetch(`${BASE}/profiles`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchProfile(name: string): Promise<ProfileResponse> {
  const res = await fetch(`${BASE}/profiles/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function createProfile(name: string): Promise<ProfileResponse> {
  const res = await fetch(`${BASE}/profiles/${encodeURIComponent(name)}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function updateProfile(
  name: string,
  values: Record<string, string>,
): Promise<ProfileResponse> {
  const res = await fetch(`${BASE}/profiles/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function renameProfile(
  name: string,
  newName: string,
): Promise<ProfileResponse> {
  const res = await fetch(`${BASE}/profiles/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ new_name: newName }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function deleteProfile(name: string): Promise<void> {
  const res = await fetch(`${BASE}/profiles/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await res.text());
}

export async function loginProfile(name: string): Promise<ProfileResponse> {
  const res = await fetch(
    `${BASE}/profiles/${encodeURIComponent(name)}/login`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// --- Claude login detection ---

export interface DetectedLogin {
  path: string;
  label: string;
  is_profile_match: boolean;
}

export interface DetectResult {
  found: DetectedLogin[];
}

export async function detectClaudeLogin(name: string): Promise<DetectResult> {
  const res = await fetch(
    `${BASE}/profiles/${encodeURIComponent(name)}/claude-login/detect`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// --- Google OAuth ---

export async function fetchGoogleStatus(
  name: string,
): Promise<GoogleStatus> {
  const res = await fetch(
    `${BASE}/profiles/${encodeURIComponent(name)}/google/status`,
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function uploadGoogleCredentials(
  name: string,
  file: File,
): Promise<GoogleStatus> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    `${BASE}/profiles/${encodeURIComponent(name)}/google/upload-credentials`,
    { method: "POST", body: form },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function authorizeGoogle(
  name: string,
): Promise<GoogleStatus> {
  const res = await fetch(
    `${BASE}/profiles/${encodeURIComponent(name)}/google/authorize`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// ── Works ────────────────────────────────────────────────────────────────────

export async function fetchWorks(): Promise<WorkSummary[]> {
  const res = await fetch(`${BASE}/works`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchWorkTree(work: string): Promise<WorkTree> {
  const res = await fetch(`${BASE}/works/${encodeURIComponent(work)}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchWorkQuestions(
  work: string,
): Promise<QuestionView[]> {
  const res = await fetch(`${BASE}/works/${encodeURIComponent(work)}/questions`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function answerWorkQuestion(
  work: string,
  question: string,
  value: string,
): Promise<QuestionView> {
  const res = await fetch(
    `${BASE}/works/${encodeURIComponent(work)}/questions/${encodeURIComponent(question)}/answer`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// The key is a path down the tree, so it stays a path in the URL rather than
// being encoded whole — the route reads it with `{key:path}`.
export async function requestWorkPart(
  work: string,
  key: string,
  reason: string,
): Promise<PartNode> {
  const res = await fetch(
    `${BASE}/works/${encodeURIComponent(work)}/parts/${key}/request`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function clearWorkPart(
  work: string,
  key: string,
): Promise<PartNode> {
  const res = await fetch(
    `${BASE}/works/${encodeURIComponent(work)}/parts/${key}/clear`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function fetchWhatItReaches(
  work: string,
  subject: string,
  kind = "term",
): Promise<ReachedBy> {
  const params = new URLSearchParams({ subject, kind });
  const res = await fetch(
    `${BASE}/works/${encodeURIComponent(work)}/reaches?${params}`,
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function recordWork(request: {
  work: string;
  chapters: string;
  title?: string;
  vocabulary?: string;
  adopt?: boolean;
}): Promise<WorkImport> {
  const res = await fetch(`${BASE}/works`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// Asked before the run rather than after: every part a pass picks up is a whole
// pipeline run, so this is what turns "start" from a blank cheque into a choice.
export async function previewWorkRun(
  work: string,
  limit = 0,
): Promise<WouldRun> {
  const params = new URLSearchParams({ limit: String(limit) });
  const res = await fetch(
    `${BASE}/works/${encodeURIComponent(work)}/would-run?${params}`,
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function startWorkRun(
  work: string,
  request: {
    passes?: number;
    limit?: number;
    concurrency?: number;
    reconcile?: boolean;
  },
): Promise<WorkLoopStatus> {
  const res = await fetch(`${BASE}/works/${encodeURIComponent(work)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function stopWorkRun(work: string): Promise<WorkLoopStatus> {
  const res = await fetch(
    `${BASE}/works/${encodeURIComponent(work)}/run/stop`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
