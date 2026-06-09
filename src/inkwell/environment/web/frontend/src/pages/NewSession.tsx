import { useCallback, useEffect, useState, type DragEvent, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { createSession, fetchFormats, fetchProfiles, uploadFile } from "../api/client";
import type { FormatOption, ProfileResponse } from "../types";

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

  useEffect(() => {
    fetchFormats().then(setFormats).catch(() => {});
    fetchProfiles()
      .then((p) => {
        setProfiles(p);
        if (p.length > 0 && !selectedProfile) setSelectedProfile(p[0].name);
      })
      .catch(() => {});
  }, []);

  const selected = formats.find((f) => f.key === format);
  const resolvedFormat = selected?.accepts_description
    ? `${format}:${customFormat}`
    : format;

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
      const result = await createSession(
        sources,
        resolvedFormat,
        refList,
        selectedProfile,
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

        {error && <div className="error-message">{error}</div>}

        <button type="submit" disabled={submitting || !hasSource || (selected?.accepts_description && !customFormat.trim())}>
          {submitting ? "Starting..." : "Start Writing"}
        </button>
      </form>
    </div>
  );
}
