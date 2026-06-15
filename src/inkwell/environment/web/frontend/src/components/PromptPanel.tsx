import { useState } from "react";
import { getSessionPrompt } from "../api/client";
import type { GeneratingPrompt } from "../types";

function toMarkdown(p: GeneratingPrompt): string {
  const parts: string[] = [];
  if (p.raw_sources.length > 0) {
    parts.push("# Sources\n\n" + p.raw_sources.join("\n"));
  }
  if (p.author_instructions.trim()) {
    parts.push("# Instructions\n\n" + p.author_instructions.trim());
  }
  if (p.author_deliverables.length > 0) {
    parts.push(
      "# Deliverables\n\n" + p.author_deliverables.map((d) => `- ${d}`).join("\n"),
    );
  }
  return parts.join("\n\n");
}

export function PromptPanel({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState<GeneratingPrompt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [copied, setCopied] = useState(false);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || loaded || loading) return;
    setLoading(true);
    try {
      setPrompt(await getSessionPrompt(sessionId));
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load source";
      setError(msg.includes("No source prompt") ? "No source recorded for this session." : msg);
    } finally {
      setLoading(false);
      setLoaded(true);
    }
  };

  const copy = async () => {
    if (!prompt) return;
    await navigator.clipboard.writeText(toMarkdown(prompt));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const download = () => {
    if (!prompt) return;
    const blob = new Blob([toMarkdown(prompt)], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `prompt-${sessionId}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="cost-breakdown prompt-panel">
      <div className="cost-breakdown-header" onClick={toggle}>
        <span>Source / Prompt</span>
        <span className={`cost-breakdown-toggle ${open ? "open" : ""}`}>&#9660;</span>
      </div>
      {open && (
        <div className="cost-breakdown-body">
          {loading && <p className="prompt-empty">Loading…</p>}
          {error && <p className="prompt-empty">{error}</p>}
          {prompt && (
            <>
              <div className="prompt-actions">
                <button className="btn-primary" onClick={copy}>
                  {copied ? "Copied" : "Copy"}
                </button>
                <button className="btn-primary" onClick={download}>
                  Download
                </button>
              </div>
              {prompt.raw_sources.length > 0 && (
                <div className="prompt-section">
                  <span className="prompt-label">Sources</span>
                  <ul className="prompt-list">
                    {prompt.raw_sources.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}
              {prompt.author_instructions.trim() && (
                <div className="prompt-section">
                  <span className="prompt-label">Instructions</span>
                  <pre className="prompt-pre">{prompt.author_instructions.trim()}</pre>
                </div>
              )}
              {prompt.author_deliverables.length > 0 && (
                <div className="prompt-section">
                  <span className="prompt-label">Deliverables</span>
                  <ul className="prompt-list">
                    {prompt.author_deliverables.map((d, i) => (
                      <li key={i}>{d}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
