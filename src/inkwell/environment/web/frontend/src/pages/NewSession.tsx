import { useCallback, useEffect, useState, type DragEvent, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  createSession,
  fetchFormats,
  fetchModelOptions,
  fetchProfiles,
  uploadFile,
} from "../api/client";
import type { FormatOption, ModelOptions, ProfileResponse } from "../types";

const CUSTOM = "__custom__";

function ModelPicker({
  value,
  onChange,
  suggested,
  emptyLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  suggested: string[];
  emptyLabel: string;
}) {
  const isKnown = value === "" || suggested.includes(value);
  const [customMode, setCustomMode] = useState(!isKnown);
  const selectValue = customMode ? CUSTOM : value;

  return (
    <div className="model-picker">
      <select
        value={selectValue}
        onChange={(e) => {
          if (e.target.value === CUSTOM) {
            setCustomMode(true);
          } else {
            setCustomMode(false);
            onChange(e.target.value);
          }
        }}
      >
        <option value="">{emptyLabel}</option>
        {suggested.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
        <option value={CUSTOM}>custom model id...</option>
      </select>
      {customMode && (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="claude-..."
        />
      )}
    </div>
  );
}

interface UploadedFile {
  serverPath: string;
  filename: string;
  size: number;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function NewSession() {
  const navigate = useNavigate();
  const [formats, setFormats] = useState<FormatOption[]>([]);
  const [profiles, setProfiles] = useState<ProfileResponse[]>([]);
  const [source, setSource] = useState("");
  const [format, setFormat] = useState("auto");
  const [customFormat, setCustomFormat] = useState("");
  const [selectedProfile, setSelectedProfile] = useState("");
  const [refs, setRefs] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [modelOptions, setModelOptions] = useState<ModelOptions | null>(null);
  const [writerMode, setWriterMode] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [stageOverrides, setStageOverrides] = useState<Record<string, string>>({});

  useEffect(() => {
    fetchFormats().then(setFormats).catch(() => {});
    fetchProfiles()
      .then((p) => {
        setProfiles(p);
        if (p.length > 0 && !selectedProfile) setSelectedProfile(p[0].name);
      })
      .catch(() => {});
    fetchModelOptions()
      .then((opts) => {
        setModelOptions(opts);
        setWriterMode(opts.default_writer_mode);
        setStageOverrides(opts.default_stage_models);
      })
      .catch(() => {});
  }, []);

  const setStageModel = (stage: string, model: string) => {
    setStageOverrides((prev) => {
      const next = { ...prev };
      if (model) next[stage] = model;
      else delete next[stage];
      return next;
    });
  };

  const selected = formats.find((f) => f.key === format);
  const resolvedFormat = selected?.accepts_description
    ? `${format}:${customFormat}`
    : format;

  const selectedProfileLogin = profiles
    .find((p) => p.name === selectedProfile)
    ?.integrations.find((i) => i.name === "Claude login");

  const handleFiles = useCallback(async (fileList: FileList) => {
    setUploading(true);
    setError(null);
    try {
      for (const file of Array.from(fileList)) {
        const result = await uploadFile(file);
        setFiles((prev) => [
          ...prev,
          { serverPath: result.path, filename: result.filename, size: result.size },
        ]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }, []);

  const handleDrop = useCallback(
    (e: DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer.files.length > 0) {
        handleFiles(e.dataTransfer.files);
      }
    },
    [handleFiles],
  );

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const hasSource = source.trim() || files.length > 0;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!hasSource) return;
    if (selected?.accepts_description && !customFormat.trim()) return;

    setSubmitting(true);
    setError(null);

    try {
      const sources: string[] = [];
      if (source.trim()) sources.push(source.trim());
      for (const f of files) sources.push(f.serverPath);

      const refList = refs
        .split("\n")
        .map((r) => r.trim())
        .filter(Boolean);
      const overrides = Object.fromEntries(
        Object.entries(stageOverrides).filter(([, v]) => v.trim()),
      );
      const result = await createSession(
        sources,
        resolvedFormat,
        refList,
        selectedProfile || profiles[0]?.name,
        {
          model: defaultModel.trim() || undefined,
          writer_mode: writerMode || undefined,
          stage_models: Object.keys(overrides).length ? overrides : undefined,
        },
      );
      navigate(`/session/${result.session_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create session");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="page new-session">
      <h1>New Writing Session</h1>
      <form onSubmit={handleSubmit}>
        {profiles.length > 0 && (
          <div className="form-group">
            <label htmlFor="profile">Profile</label>
            <select
              id="profile"
              value={selectedProfile}
              onChange={(e) => setSelectedProfile(e.target.value)}
            >
              {profiles.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
            {selectedProfileLogin && !selectedProfileLogin.configured && (
              <p className="form-note warning" style={{ color: "#c0392b" }}>
                ⚠ This profile has no separate Claude login — inference will bill
                the server's default account, not this profile. Add one under
                Settings → {selectedProfile} → Claude login.
              </p>
            )}
            {selectedProfileLogin?.configured && (
              <p className="form-note" style={{ opacity: 0.7 }}>
                Inference billed to this profile's Claude login.
              </p>
            )}
          </div>
        )}

        <div className="form-group">
          <label htmlFor="source">Source Material</label>
          <textarea
            id="source"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="Paste a Claude share link, URL, or freeform text..."
            rows={4}
          />
        </div>

        <div className="form-group">
          <label>Upload Files</label>
          <div
            className={`file-drop-zone${dragOver ? " drag-over" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            onClick={() => document.getElementById("file-input")?.click()}
          >
            <input
              id="file-input"
              type="file"
              accept=".pdf,.md,.txt,.html,.htm"
              multiple
              hidden
              onChange={(e) => {
                if (e.target.files?.length) handleFiles(e.target.files);
                e.target.value = "";
              }}
            />
            {uploading ? (
              <span className="drop-label">Uploading...</span>
            ) : (
              <span className="drop-label">
                Drop PDFs, markdown, or text files here — or click to browse
              </span>
            )}
          </div>

          {files.length > 0 && (
            <div className="file-list">
              {files.map((f, i) => (
                <div key={f.serverPath} className="file-chip">
                  <span className="file-chip-name">{f.filename}</span>
                  <span className="file-chip-size">{formatBytes(f.size)}</span>
                  <button
                    type="button"
                    className="file-chip-remove"
                    onClick={(e) => { e.stopPropagation(); removeFile(i); }}
                  >
                    &times;
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="form-group">
          <label htmlFor="format">Output Format</label>
          <select
            id="format"
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            {formats.map((f) => (
              <option key={f.key} value={f.key}>
                {f.accepts_description ? `${f.label}...` : f.label}
              </option>
            ))}
          </select>
          {selected?.accepts_description && (
            <textarea
              id="custom-format"
              value={customFormat}
              onChange={(e) => setCustomFormat(e.target.value)}
              placeholder="Describe the desired format — tone, structure, audience, conventions. E.g. 'academic abstract with keywords' or 'diplomatic cable with numbered paragraphs'"
              rows={3}
              required
            />
          )}
        </div>

        <div className="form-group">
          <label htmlFor="refs">References (one per line, optional)</label>
          <textarea
            id="refs"
            value={refs}
            onChange={(e) => setRefs(e.target.value)}
            placeholder="https://arxiv.org/abs/...\nhttps://example.com/paper.pdf"
            rows={3}
          />
        </div>

        {modelOptions && (
          <details className="model-config">
            <summary>
              Models &amp; pipeline
              <span className="model-config-hint">
                {defaultModel || modelOptions.default_model}
                {writerMode ? ` · ${writerMode} writer` : ""}
                {Object.values(stageOverrides).some((v) => v.trim())
                  ? ` · ${Object.values(stageOverrides).filter((v) => v.trim()).length} stage override(s)`
                  : ""}
              </span>
            </summary>

            <div className="form-group">
              <label htmlFor="writer-mode">Writer mode</label>
              <select
                id="writer-mode"
                value={writerMode}
                onChange={(e) => setWriterMode(e.target.value)}
              >
                {modelOptions.writer_modes.map((m) => (
                  <option key={m} value={m}>
                    {m === "single"
                      ? "single — one writer drafts the whole piece (no merge stage)"
                      : "parallel — one writer per section, then merge"}
                  </option>
                ))}
              </select>
            </div>

            <div className="form-group">
              <label>Default model (all stages)</label>
              <ModelPicker
                value={defaultModel}
                onChange={setDefaultModel}
                suggested={modelOptions.suggested_models}
                emptyLabel={`profile default (${modelOptions.default_model})`}
              />
            </div>

            <div className="form-group">
              <label>Per-stage overrides</label>
              <div className="stage-model-grid">
                {modelOptions.stages.map((stage) => (
                  <div key={stage} className="stage-model-row">
                    <span className="stage-model-name">{stage}</span>
                    <ModelPicker
                      value={stageOverrides[stage] ?? ""}
                      onChange={(v) => setStageModel(stage, v)}
                      suggested={modelOptions.suggested_models}
                      emptyLabel="default"
                    />
                  </div>
                ))}
              </div>
            </div>
          </details>
        )}

        {error && <div className="error-message">{error}</div>}

        <button type="submit" disabled={submitting || !hasSource || (selected?.accepts_description && !customFormat.trim())}>
          {submitting ? "Starting..." : "Start Writing"}
        </button>
      </form>
    </div>
  );
}
