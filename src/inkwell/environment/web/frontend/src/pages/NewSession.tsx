import { useCallback, useEffect, useState, type DragEvent, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  fetchEntryPoints,
  fetchFormats,
  fetchModelOptions,
  fetchProfiles,
  startSession,
  uploadFile,
  uploadText,
} from "../api/client";
import { DeclaredFields } from "../components/DeclaredFields";
import type {
  EntryPointDescriptor,
  FormatOption,
  ModelOptions,
  ParameterDescriptor,
  ProfileResponse,
  SuppliedValue,
  SuppliedValues,
} from "../types";
import { carries, declaredDefaults } from "../types";

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
  text?: string;
}

interface ComposerTarget {
  area: "source" | "reference";
  editIndex: number | null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Attachments live only in component state, so an HMR update or accidental
// reload between upload and submit drops them while the uploaded file lingers
// on the server — the silent loss this guards against. Persist them so a
// remount restores the chips instead of leaving them behind.
const FILES_KEY = "inkwell.newSession.files";
const REF_FILES_KEY = "inkwell.newSession.refFiles";

function loadStoredFiles(key: string): UploadedFile[] {
  try {
    const raw = sessionStorage.getItem(key);
    return raw ? (JSON.parse(raw) as UploadedFile[]) : [];
  } catch {
    return [];
  }
}

function TextFileComposer({
  initialName,
  initialText,
  placeholder,
  saving,
  error,
  onSave,
  onCancel,
}: {
  initialName: string;
  initialText: string;
  placeholder: string;
  saving: boolean;
  error: string | null;
  onSave: (name: string, text: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initialName);
  const [text, setText] = useState(initialText);

  return (
    <div className="text-composer">
      <input
        className="text-composer-name"
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="filename (e.g. notes.md)"
      />
      <textarea
        className="text-composer-body"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        rows={6}
        autoFocus
      />
      <div className="text-composer-actions">
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="btn-primary"
          disabled={saving || !text.trim()}
          onClick={() => onSave(name.trim(), text)}
        >
          {saving ? "Saving..." : "Save file"}
        </button>
      </div>
      {error && <div className="error-message text-composer-error">{error}</div>}
    </div>
  );
}

function FileChips({
  files,
  onEdit,
  onRemove,
}: {
  files: UploadedFile[];
  onEdit: (index: number) => void;
  onRemove: (index: number) => void;
}) {
  if (files.length === 0) return null;
  return (
    <div className="file-list">
      {files.map((f, i) => (
        <div key={f.serverPath} className="file-chip">
          <span className="file-chip-name">{f.filename}</span>
          <span className="file-chip-size">{formatBytes(f.size)}</span>
          {f.text !== undefined && (
            <button
              type="button"
              className="file-chip-edit"
              title="Edit text"
              onClick={(e) => {
                e.stopPropagation();
                onEdit(i);
              }}
            >
              ✎
            </button>
          )}
          <button
            type="button"
            className="file-chip-remove"
            onClick={(e) => {
              e.stopPropagation();
              onRemove(i);
            }}
          >
            &times;
          </button>
        </div>
      ))}
    </div>
  );
}

export function NewSession() {
  const navigate = useNavigate();
  const [entryPoints, setEntryPoints] = useState<EntryPointDescriptor[]>([]);
  const [entryPointName, setEntryPointName] = useState("write");
  const [declared, setDeclared] = useState<SuppliedValues>({});
  const [formats, setFormats] = useState<FormatOption[]>([]);
  const [profiles, setProfiles] = useState<ProfileResponse[]>([]);
  const [source, setSource] = useState("");
  const [format, setFormat] = useState("auto");
  const [customFormat, setCustomFormat] = useState("");
  const [selectedProfile, setSelectedProfile] = useState("");
  const [refs, setRefs] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [files, setFiles] = useState<UploadedFile[]>(() => loadStoredFiles(FILES_KEY));
  const [refFiles, setRefFiles] = useState<UploadedFile[]>(() =>
    loadStoredFiles(REF_FILES_KEY),
  );
  const [composer, setComposer] = useState<ComposerTarget | null>(null);
  const [composerError, setComposerError] = useState<string | null>(null);
  const [savingText, setSavingText] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [modelOptions, setModelOptions] = useState<ModelOptions | null>(null);
  const [writerMode, setWriterMode] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [stageOverrides, setStageOverrides] = useState<Record<string, string>>({});

  const entryPoint =
    entryPoints.find((e) => e.name === entryPointName) ?? null;
  // Only the entry points that start something new belong on this page; resume
  // and restart act on a session that already exists. The declaration says
  // which is which, so this does not turn on a parameter one happens to take.
  const startable = entryPoints.filter((e) => e.starts_a_session);

  useEffect(() => {
    fetchEntryPoints()
      .then((loaded) => {
        setEntryPoints(loaded);
        const write = loaded.find((entry) => entry.name === "write") ?? null;
        setDeclared(declaredDefaults(write));
      })
      .catch(() => {});
    fetchFormats().then(setFormats).catch(() => {});
    fetchProfiles()
      .then((p) => {
        setProfiles(p);
        if (p.length > 0) setSelectedProfile((current) => current || p[0].name);
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

  useEffect(() => {
    sessionStorage.setItem(FILES_KEY, JSON.stringify(files));
  }, [files]);

  useEffect(() => {
    sessionStorage.setItem(REF_FILES_KEY, JSON.stringify(refFiles));
  }, [refFiles]);

  const setDeclaredValue = (name: string, next: SuppliedValue) => {
    setDeclared((prev) => ({ ...prev, [name]: next }));
  };

  const selectEntryPoint = (name: string) => {
    setEntryPointName(name);
    const selected = entryPoints.find((entry) => entry.name === name) ?? null;
    setDeclared(declaredDefaults(selected));
  };

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

  // Opening a fresh composer must drop a previous save's error so it doesn't
  // greet the next paste; closing does too.
  const openComposer = (target: ComposerTarget) => {
    setComposerError(null);
    setComposer(target);
  };

  const closeComposer = () => {
    setComposerError(null);
    setComposer(null);
  };

  // Removing a chip shifts indices, so an open edit of that area would now
  // point at the wrong file — cancel it. A new paste (editIndex null) keeps
  // its draft.
  const cancelEditOn = (area: ComposerTarget["area"]) => {
    if (composer?.area === area && composer.editIndex !== null) setComposer(null);
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    cancelEditOn("source");
  };

  const removeRefFile = (index: number) => {
    setRefFiles((prev) => prev.filter((_, i) => i !== index));
    cancelEditOn("reference");
  };

  const saveTextFile = async (name: string, text: string) => {
    if (!composer) return;
    const { area, editIndex } = composer;
    const list = area === "source" ? files : refFiles;
    const setList = area === "source" ? setFiles : setRefFiles;
    const replacePath = editIndex !== null ? list[editIndex]?.serverPath : undefined;

    setSavingText(true);
    setComposerError(null);
    try {
      const result = await uploadText(name, text, replacePath);
      const entry: UploadedFile = {
        serverPath: result.path,
        filename: result.filename,
        size: result.size,
        text,
      };
      setList((prev) =>
        editIndex === null
          ? [...prev, entry]
          : prev.map((f, i) => (i === editIndex ? entry : f)),
      );
      setComposer(null);
    } catch (err) {
      setComposerError(err instanceof Error ? err.message : "Failed to save text");
    } finally {
      setSavingText(false);
    }
  };

  const takesSources = carries(entryPoint, "sources");
  const takesRefs = carries(entryPoint, "refs");
  const hasSource = Boolean(source.trim()) || files.length > 0;

  // A required parameter is satisfied by whatever surface carries it: the
  // source picker for `sources`, the generic control for anything else.
  const isSupplied = (parameter: ParameterDescriptor): boolean => {
    if (!parameter.rendered) return parameter.name === "sources" ? hasSource : true;
    const value = declared[parameter.name];
    if (typeof value === "string") return value.trim().length > 0;
    if (Array.isArray(value)) return value.length > 0;
    return value !== null && value !== undefined;
  };

  const needsDescription = Boolean(
    selected?.accepts_description && !customFormat.trim(),
  );
  const ready =
    entryPoint !== null &&
    entryPoint.parameters.filter((p) => p.required).every(isSupplied) &&
    !needsDescription;

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ready || !entryPoint) return;
    if (composer !== null) {
      setError("Save or cancel your pasted text first — it isn't attached yet.");
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      const sources: string[] = [];
      if (source.trim()) sources.push(source.trim());
      for (const f of files) sources.push(f.serverPath);

      const refList = [
        ...refs
          .split("\n")
          .map((r) => r.trim())
          .filter(Boolean),
        ...refFiles.map((f) => f.serverPath),
      ];
      const overrides = Object.fromEntries(
        Object.entries(stageOverrides).filter(([, v]) => v.trim()),
      );

      // The generic controls' values, plus the bespoke ones under the same
      // declared names the descriptor gave them.
      const bespoke: SuppliedValues = {
        profile: selectedProfile || profiles[0]?.name || null,
        model: defaultModel.trim() || null,
        writer_mode: writerMode || null,
        stage_models: overrides,
      };
      if (takesSources) bespoke.sources = sources;
      if (takesRefs) bespoke.refs = refList;
      if (carries(entryPoint, "target_format")) {
        bespoke.target_format = resolvedFormat;
      }

      const result = await startSession(entryPoint.name, {
        ...declared,
        ...bespoke,
      });
      sessionStorage.removeItem(FILES_KEY);
      sessionStorage.removeItem(REF_FILES_KEY);
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
        {startable.length > 1 && (
          <div className="form-group">
            <label htmlFor="entry-point">Start from</label>
            <select
              id="entry-point"
              value={entryPointName}
              onChange={(e) => selectEntryPoint(e.target.value)}
            >
              {startable.map((e) => (
                <option key={e.name} value={e.name}>
                  {e.summary}
                </option>
              ))}
            </select>
          </div>
        )}

        {profiles.length > 0 && carries(entryPoint, "profile") && (
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

        {takesSources && (
          <>
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

              {composer?.area === "source" ? (
                <TextFileComposer
                  key={`source-${composer.editIndex ?? "new"}`}
                  initialName={
                    composer.editIndex !== null
                      ? (files[composer.editIndex]?.filename ?? "")
                      : ""
                  }
                  initialText={
                    composer.editIndex !== null
                      ? (files[composer.editIndex]?.text ?? "")
                      : ""
                  }
                  placeholder="Paste or type text — saved as a source file the pipeline ingests like an upload"
                  saving={savingText}
                  error={composerError}
                  onSave={saveTextFile}
                  onCancel={closeComposer}
                />
              ) : (
                <button
                  type="button"
                  className="paste-text-button"
                  onClick={() => openComposer({ area: "source", editIndex: null })}
                >
                  + Paste text as file
                </button>
              )}

              <FileChips
                files={files}
                onEdit={(i) => openComposer({ area: "source", editIndex: i })}
                onRemove={removeFile}
              />
            </div>
          </>
        )}

        <DeclaredFields
          parameters={entryPoint?.parameters ?? []}
          values={declared}
          onChange={setDeclaredValue}
          only={["task", "draft"]}
        />

        {carries(entryPoint, "target_format") && (
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
        )}

        {takesRefs && (
          <div className="form-group">
            <label htmlFor="refs">References (one per line, optional)</label>
            <textarea
              id="refs"
              value={refs}
              onChange={(e) => setRefs(e.target.value)}
              placeholder="https://arxiv.org/abs/...\nhttps://example.com/paper.pdf"
              rows={3}
            />

            {composer?.area === "reference" ? (
              <TextFileComposer
                key={`reference-${composer.editIndex ?? "new"}`}
                initialName={
                  composer.editIndex !== null
                    ? (refFiles[composer.editIndex]?.filename ?? "")
                    : ""
                }
                initialText={
                  composer.editIndex !== null
                    ? (refFiles[composer.editIndex]?.text ?? "")
                    : ""
                }
                placeholder="Paste or type reference text — saved as a file and passed as a reference"
                saving={savingText}
                error={composerError}
                onSave={saveTextFile}
                onCancel={closeComposer}
              />
            ) : (
              <button
                type="button"
                className="paste-text-button"
                onClick={() => openComposer({ area: "reference", editIndex: null })}
              >
                + Paste text as file
              </button>
            )}

            <FileChips
              files={refFiles}
              onEdit={(i) => openComposer({ area: "reference", editIndex: i })}
              onRemove={removeRefFile}
            />
          </div>
        )}

        <DeclaredFields
          parameters={(entryPoint?.parameters ?? []).filter(
            (p) => p.name !== "task" && p.name !== "draft",
          )}
          values={declared}
          onChange={setDeclaredValue}
        />

        {modelOptions && carries(entryPoint, "model") && (
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

        <button type="submit" disabled={submitting || !ready}>
          {submitting ? "Starting..." : "Start Writing"}
        </button>
      </form>
    </div>
  );
}
