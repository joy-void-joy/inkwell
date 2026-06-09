import { useEffect, useRef, useCallback } from "react";
import type { ServerMessage } from "../types";

type MessageHandler = (msg: ServerMessage) => void;

const RECONNECT_DELAYS = [500, 1000, 2000, 4000, 8000];
const MAX_RECONNECT_ATTEMPTS = 10;
const WS_CLOSE_SESSION_NOT_FOUND = 4004;

export function useSessionWebSocket(
  sessionId: string | undefined,
  onMessage: MessageHandler,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const mountedRef = useRef(true);
  const stopRef = useRef(false);
  const prevSessionRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    console.log("[ws] effect fired, sessionId=", sessionId, "stopRef=", stopRef.current);
    console.trace("[ws] effect caller");
    mountedRef.current = true;
    if (prevSessionRef.current !== sessionId) {
      stopRef.current = false;
      attemptRef.current = 0;
      prevSessionRef.current = sessionId;
    }
    if (!sessionId || stopRef.current) return;

    function connect() {
      if (!mountedRef.current || !sessionId || stopRef.current) return;

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws/${sessionId}`);
      wsRef.current = ws;

      ws.onopen = () => {};

      ws.onmessage = (event) => {
        try {
          const msg: ServerMessage = JSON.parse(event.data);
          if (msg.type === "error" && msg.message === "Session not found") {
            stopRef.current = true;
            onMessage(msg);
            return;
          }
          if (msg.type === "session_ended") {
            stopRef.current = true;
          }
          if (!stopRef.current) {
            attemptRef.current = 0;
          }
          onMessage(msg);
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        if (!mountedRef.current) return;
        if (stopRef.current) return;
        if (event.code === WS_CLOSE_SESSION_NOT_FOUND) return;
        if (attemptRef.current >= MAX_RECONNECT_ATTEMPTS) return;
        const delay = RECONNECT_DELAYS[Math.min(attemptRef.current, RECONNECT_DELAYS.length - 1)];
        attemptRef.current++;
        setTimeout(connect, delay);
      };

      ws.onerror = () => {
        ws.close();
      };
    }

    connect();

    return () => {
      mountedRef.current = false;
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [sessionId, onMessage]);

  const send = useCallback((msg: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  return { send };
}
