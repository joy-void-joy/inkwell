import { useState, useRef, useEffect, type FormEvent, type KeyboardEvent } from "react";

interface ActionBarProps {
  onSend: (text: string) => void;
  onSync?: () => void;
  onStop?: () => void;
  placeholder?: string;
  disabled: boolean;
}

export function ActionBar({
  onSend,
  onSync,
  onStop,
  placeholder = "Send feedback... (Enter to send)",
  disabled,
}: ActionBarProps) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  }, [text]);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    onSend(text.trim());
    setText("");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (text.trim()) {
        handleSubmit(e as unknown as FormEvent);
      }
    }
  };

  return (
    <div className="action-bar">
      <form onSubmit={handleSubmit} className="feedback-form">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={disabled}
          rows={1}
        />
        <div className="action-buttons">
          <button type="submit" disabled={disabled || !text.trim()} className="btn-primary">
            Send
          </button>
          {onSync && (
            <button type="button" onClick={onSync} disabled={disabled}>Sync</button>
          )}
          {onStop && (
            <button type="button" onClick={onStop} className="btn-danger">Stop</button>
          )}
        </div>
      </form>
    </div>
  );
}
