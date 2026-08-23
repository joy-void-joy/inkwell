import { useRef, useEffect, useState } from "react";
import type { ServerMessage } from "../types";
import { stageLabel } from "../types";

function formatEvent(event: ServerMessage): string {
  switch (event.type) {
    case "stage":
      return event.description;
    case "progress":
      return event.message;
    case "message":
      return event.message;
    case "block":
      return event.content;
    case "agent":
      return event.agent.error || event.agent.status;
    case "error":
      return event.message;
    case "complete":
      return `${event.output.title} (${event.output.word_count} words)`;
    case "session_ended":
      return `Session ${event.status}`;
    case "cost_update":
    case "state_update":
    case "collect_revision":
      return "";
  }
}

function eventLabel(event: ServerMessage): string {
  switch (event.type) {
    case "stage":
      return stageLabel(event.stage);
    case "message":
      return event.source;
    case "block":
      return event.block_type;
    case "agent":
      return event.agent.address;
    case "complete":
      return "Done";
    case "session_ended":
      return "End";
    default:
      return "";
  }
}

function blockCssClass(event: ServerMessage): string {
  if (event.type !== "block") return `log-${event.type}`;
  const bt = event.block_type.toLowerCase();
  if (bt === "thinking") return "log-thinking";
  if (bt === "response") return "log-response";
  if (bt.startsWith("tool:")) return "log-tool";
  if (bt === "result") return "log-result";
  return "log-block";
}

function BlockContent({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const threshold = 300;
  const needsTruncation = text.length > threshold;

  if (!needsTruncation || expanded) {
    return (
      <span className="log-text log-text-pre">
        {text}
        {needsTruncation && (
          <button className="log-toggle" onClick={() => setExpanded(false)}>less</button>
        )}
      </span>
    );
  }

  return (
    <span className="log-text log-text-pre">
      {text.slice(0, threshold)}
      <button className="log-toggle" onClick={() => setExpanded(true)}>more</button>
    </span>
  );
}

export function LogStream({ events, stopped = false }: { events: ServerMessage[]; stopped?: boolean }) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  const visible = events.filter(
    (e) => e.type !== "cost_update" && e.type !== "state_update" && e.type !== "collect_revision" && formatEvent(e),
  );

  return (
    <div className="log-stream">
      <div className="log-header">
        <span>Activity</span>
        {visible.length > 0 && <span>{visible.length} events</span>}
      </div>
      <div className="log-entries">
        {visible.length === 0 && !stopped && (
          <div className="log-empty">Waiting for pipeline events…</div>
        )}
        {visible.length === 0 && stopped && (
          <div className="log-empty">No events recorded. Use Resume to continue this session.</div>
        )}
        {visible.map((event, i) => {
          const label = eventLabel(event);
          const isBlock = event.type === "block";
          return (
            <div key={i} className={`log-entry ${blockCssClass(event)}`}>
              <span className="log-time">
                {new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
              {label && <span className="log-label">{label}</span>}
              {isBlock ? (
                <BlockContent text={formatEvent(event)} />
              ) : (
                <span className="log-text">{formatEvent(event)}</span>
              )}
            </div>
          );
        })}
        <div ref={endRef} />
      </div>
    </div>
  );
}
