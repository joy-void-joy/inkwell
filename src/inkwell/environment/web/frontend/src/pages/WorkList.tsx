import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchFormats, fetchWorks, recordWork } from "../api/client";
import type { FormatOption, WorkSummary } from "../types";

// A work is not a session and is listed apart from one. A session has a
// beginning and an end; a work outlives every run against it and is what an
// author comes back to.
//
// The import form is here because the alternative was a page that told you to
// go and use a terminal — which made the browser a reader of what something
// else had set up rather than somewhere the work is managed.

const POLL_MS = 10000;

// The format is asked for here rather than on the run panel because it belongs
// to the work, not to a run: a book whose third pass writes to different rules
// than its first is a book with a seam through it. Defaulted to what the server
// defaults to, so the common case is one less decision.
const FORMAT_DEFAULT = "textbook";

export function WorkList() {
  const [works, setWorks] = useState<WorkSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [reloads, setReloads] = useState(0);

  const [importing, setImporting] = useState(false);
  const [work, setWork] = useState("");
  const [chapters, setChapters] = useState("");
  const [title, setTitle] = useState("");
  const [targetFormat, setTargetFormat] = useState(FORMAT_DEFAULT);
  const [formats, setFormats] = useState<FormatOption[]>([]);
  const [adopt, setAdopt] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recorded, setRecorded] = useState("");

  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const loaded = await fetchWorks();
        if (live) setWorks(loaded);
      } finally {
        if (live) setLoading(false);
      }
    };
    load();
    const interval = setInterval(load, POLL_MS);
    return () => {
      live = false;
      clearInterval(interval);
    };
  }, [reloads]);

  // Read off the same declaration the new-session page offers, so a format
  // added to the catalogue reaches both surfaces without either being edited.
  useEffect(() => {
    fetchFormats()
      .then(setFormats)
      .catch(() => {});
  }, []);

  const submit = async () => {
    setBusy(true);
    setError("");
    setRecorded("");
    try {
      const result = await recordWork({
        work,
        chapters,
        title,
        target_format: targetFormat,
        adopt,
      });
      setRecorded(
        `${result.title}: ${result.parts} part(s) as ${result.target_format}, ` +
          `${result.vocabulary} declared term(s), ${result.dependencies} dependencies, ` +
          `${result.adopted} stamped as built`,
      );
      setWork("");
      setChapters("");
      setTitle("");
      setTargetFormat(FORMAT_DEFAULT);
      setImporting(false);
      setReloads((held) => held + 1);
    } catch (failure) {
      setError(String(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page work-list">
      <div className="page-header">
        <h1>Works</h1>
        <Link to="/" className="back-link">
          Sessions
        </Link>
      </div>

      {loading && <p>Loading...</p>}

      {!loading && works.length === 0 && (
        <p>No work is recorded yet. Import one below.</p>
      )}

      {works.map((held) => (
        <Link key={held.id} to={`/work/${held.id}`} className="work-card">
          <strong>{held.title}</strong>
          <span>
            {held.parts} part(s) ·{" "}
            {held.outstanding === 0
              ? "settled"
              : `${held.outstanding} outstanding`}
          </span>
        </Link>
      ))}

      {recorded && <p className="work-recorded">{recorded}</p>}

      <section className="work-import">
        {!importing && (
          <button onClick={() => setImporting(true)}>Import a work</button>
        )}
        {importing && (
          <>
            <h2>Import a work</h2>
            <p>
              The directory is read on this server, because that is where every
              run against the work will look for it.
            </p>
            <label>
              Chapters directory
              <input
                autoFocus
                value={chapters}
                placeholder="/path/to/textbook/docs/chapters"
                onChange={(event) => setChapters(event.target.value)}
              />
            </label>
            <label>
              Record it as
              <input
                value={work}
                placeholder="atlas"
                onChange={(event) => setWork(event.target.value)}
              />
              <small>Lowercase words, hyphenated — it names a directory</small>
            </label>
            <label>
              Called
              <input
                value={title}
                placeholder="AI Safety Atlas"
                onChange={(event) => setTitle(event.target.value)}
              />
            </label>
            <label>
              Written as
              <select
                value={targetFormat}
                onChange={(event) => setTargetFormat(event.target.value)}
              >
                {formats.length === 0 && (
                  <option value={FORMAT_DEFAULT}>{FORMAT_DEFAULT}</option>
                )}
                {formats.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label}
                  </option>
                ))}
              </select>
              <small>
                Every run against this work inherits it. A work of chapters is a
                textbook — leaving it on <code>auto</code> asks each part to
                guess the format of a book it sees one subsection of.
              </small>
            </label>
            <label className="run-checkbox">
              <input
                type="checkbox"
                checked={adopt}
                onChange={(event) => setAdopt(event.target.checked)}
              />
              Take the text it already holds as built
            </label>
            <small>
              Leaving this off treats every part as unwritten, which makes the
              first pass a rewrite of the whole work.
            </small>
            <div className="run-actions">
              <button
                onClick={submit}
                disabled={busy || !work.trim() || !chapters.trim()}
              >
                Import
              </button>
              <button onClick={() => setImporting(false)} disabled={busy}>
                Cancel
              </button>
            </div>
            {error && <p className="run-error">{error}</p>}
          </>
        )}
      </section>
    </div>
  );
}
