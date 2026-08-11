import { createContext, useContext } from "react";
import type {
  CostSnapshot,
  SessionStateSnapshot,
  CompletionOutput,
  ServerMessage,
  SessionStatus,
} from "../types";

export interface SessionState {
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

export interface SessionContextValue {
  state: SessionState;
  pipelineStages: string[];
  send: (text: string) => void;
  sendAction: (action: string) => void;
  resume: (fromStage?: string, profile?: string, stopAfter?: string) => Promise<void>;
  restart: (fromStage: string, profile?: string) => Promise<void>;
}

/**
 * The running session a subtree is reading, apart from the component that
 * provides it: fast refresh only reloads a module that exports components
 * alone, so the context and its hook live here and the provider next door.
 */
export const SessionContext = createContext<SessionContextValue | null>(null);

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be inside SessionProvider");
  return ctx;
}
