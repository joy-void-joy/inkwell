import type {
  FormatOption,
  GoogleStatus,
  ModelConfig,
  ModelOptions,
  ProfileResponse,
  ServerCapabilities,
  SessionDetail,
  SessionSummary,
} from "../types";

const BASE = "/api";

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

export async function createSession(
  sources: string[],
  targetFormat: string = "auto",
  refs: string[] = [],
  profile?: string,
  modelConfig?: ModelConfig,
): Promise<{ session_id: string; status: string }> {
  const res = await fetch(`${BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sources,
      target_format: targetFormat,
      refs,
      profile: profile || null,
      model: modelConfig?.model || null,
      stage_models: modelConfig?.stage_models || {},
      writer_mode: modelConfig?.writer_mode || null,
    }),
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

export async function resumeSession(
  id: string,
  fromStage?: string,
  profile?: string,
  modelConfig?: ModelConfig,
): Promise<{ session_id: string; status: string }> {
  const res = await fetch(`${BASE}/sessions/${id}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      from_stage: fromStage ?? null,
      profile: profile ?? null,
      model: modelConfig?.model || null,
      stage_models: modelConfig?.stage_models || {},
      writer_mode: modelConfig?.writer_mode || null,
    }),
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
