import { createContext, useContext, useReducer, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useSessionWebSocket } from "../api/ws";
import { getSession, resumeSession, restartSession, fetchPipelineStages } from "../api/client";
import type {
  CostSnapshot,
  SessionStateSnapshot,
  CompletionOutput,
  ServerMessage,
  SessionStatus,
} from "../types";

interface SessionState {
  sessionId: string;
  title: string;
  status: SessionStatus;
  stage: string;
  profile: string | null;
  sessionState: SessionStateSnapshot | null;
  cost: CostSnapshot | null;
  output: CompletionOutput | null;
  events: ServerMessage[];
  awaitingRevision: boolean;
  error: string | null;
  startedAt: string | null;
}

type Action =
  | { type: "INIT"; detail: { status: SessionStatus; title: string; state: SessionStateSnapshot; cost: CostSnapshot; created_at: string; profile: string | null; events: ServerMessage[]; output?: CompletionOutput | null } }
  | { type: "RESUMING" }
  | { type: "RESUMED" }
  | { type: "EVENT"; event: ServerMessage };

function reducer(state: SessionState, action: Action): SessionState {
  switch (action.type) {
    case "INIT": {
      if (state.events.length > 0) return state;
      const { detail } = action;
      let s: SessionState = {
        ...state,
        status: detail.status,
        title: detail.title || state.title,
        sessionState: detail.state,
        cost: detail.cost,
        stage: detail.state.stage,
        profile: detail.profile || state.profile,
        startedAt: detail.created_at || state.startedAt,
        output: detail.output ?? state.output,
      };
      for (const event of detail.events) {
        s = reducer(s, { type: "EVENT", event });
      }
      return s;
    }
    case "RESUMING":
      return { ...state, status: "resuming" };
    case "RESUMED":
      return { ...state, status: "running", events: [], error: null };
    case "EVENT": {
      const event = action.event;
      const startedAt = state.startedAt ?? event.timestamp;
      const events = [...state.events, event];
      switch (event.type) {
        case "stage":
          return { ...state, events, stage: event.stage, awaitingRevision: false, startedAt };
        case "progress":
        case "message":
        case "block":
          return { ...state, events, startedAt };
        case "cost_update":
          return { ...state, cost: event.cost, startedAt };
        case "state_update":
          return { ...state, sessionState: event.state, title: event.state.title || state.title, startedAt };
        case "complete":
          return { ...state, events, output: event.output, startedAt };
        case "collect_revision":
          return { ...state, events, awaitingRevision: true, sessionState: event.state, startedAt };
        case "error":
          return { ...state, events, error: event.message, startedAt };
        case "session_ended":
          return { ...state, events, status: event.status, awaitingRevision: false, startedAt };
        default:
          return { ...state, events, startedAt };
      }
    }
  }
}

function initialState(sessionId: string): SessionState {
  return {
    sessionId,
    title: "",
    status: "running",
    stage: "starting",
    profile: null,
    sessionState: null,
    cost: null,
    output: null,
    events: [],
    awaitingRevision: false,
    error: null,
    startedAt: null,
  };
}

interface SessionContextValue {
  state: SessionState;
  pipelineStages: string[];
  send: (text: string) => void;
  sendAction: (action: string) => void;
  resume: (fromStage?: string, profile?: string, stopAfter?: string) => Promise<void>;
  restart: (fromStage: string, profile?: string) => Promise<void>;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({
  sessionId,
  children,
}: {
  sessionId: string;
  children: ReactNode;
}) {
  const [state, dispatch] = useReducer(reducer, sessionId, initialState);
  const seededRef = useRef(false);
  const [wsSessionId, setWsSessionId] = useState<string | undefined>(undefined);
  const [pipelineStages, setPipelineStages] = useState<string[]>([]);

  useEffect(() => {
    fetchPipelineStages().then(setPipelineStages).catch(() => {});
  }, []);

  const onMessage = useCallback((msg: ServerMessage) => {
    dispatch({ type: "EVENT", event: msg });
  }, []);

  const prevOnMessageRef = useRef(onMessage);
  if (prevOnMessageRef.current !== onMessage) {
    console.warn("[SessionProvider] onMessage ref CHANGED — this causes WS reconnect");
    prevOnMessageRef.current = onMessage;
  }

  const { send } = useSessionWebSocket(wsSessionId, onMessage);

  useEffect(() => {
    if (seededRef.current) return;
    let cancelled = false;
    (async () => {
      try {
        const detail = await getSession(sessionId);
        if (cancelled || seededRef.current) return;
        seededRef.current = true;
        dispatch({ type: "INIT", detail });
        if (detail.status === "running") {
          setWsSessionId(sessionId);
        }
      } catch {
        // GET failed — don't blindly open a WebSocket; the session may not exist
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

  useEffect(() => {
    if (state.events.length > 0) {
      seededRef.current = true;
    }
  }, [state.events.length]);

  const resume = useCallback(async (fromStage?: string, profile?: string, stopAfter?: string) => {
    await resumeSession(sessionId, fromStage, profile, undefined, stopAfter);
    dispatch({ type: "RESUMED" });
    setWsSessionId(sessionId);
  }, [sessionId]);

  const restart = useCallback(async (fromStage: string, profile?: string) => {
    await restartSession(sessionId, fromStage, profile);
    dispatch({ type: "RESUMED" });
    setWsSessionId(sessionId);
  }, [sessionId]);

  const sendText = useCallback(
    async (text: string) => {
      if (state.status !== "running") {
        dispatch({ type: "RESUMING" });
        await resumeSession(sessionId);
        dispatch({ type: "RESUMED" });
        setWsSessionId(sessionId);
        setTimeout(() => send({ type: "feedback", items: [text] }), 500);
      } else if (state.awaitingRevision) {
        send({ type: "revision", text });
      } else {
        send({ type: "feedback", items: [text] });
      }
    },
    [state.status, state.awaitingRevision, send, sessionId],
  );

  const sendAction = useCallback(
    (action: string) => send({ type: "action", action }),
    [send],
  );

  return (
    <SessionContext.Provider value={{ state, pipelineStages, send: sendText, sendAction, resume, restart }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be inside SessionProvider");
  return ctx;
}
