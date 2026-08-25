import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import * as api from "../api/client";
import {
  completedStageNames,
  type PartNode,
  type ServerMessage,
  type WorkLoopStatus,
  type WorkTree,
} from "../types";
import { SessionProvider } from "../context/SessionContext";
import { useSession } from "../context/session";
import { WorkTreeView } from "./WorkTreeView";

const socket = vi.hoisted(() => ({
  deliver: null as ((event: ServerMessage) => void) | null,
  send: vi.fn(),
}));

vi.mock("../api/client", () => ({
  answerWorkQuestion: vi.fn(),
  clearWorkPart: vi.fn(),
  fetchWhatItReaches: vi.fn(),
  fetchWorkActivity: vi.fn(),
  fetchWorkHistory: vi.fn(),
  fetchWorkQuestions: vi.fn(),
  fetchWorkTree: vi.fn(),
  fetchPipelineStages: vi.fn(),
  getSession: vi.fn(),
  requestWorkPart: vi.fn(),
  resumeSession: vi.fn(),
  startWorkPart: vi.fn(),
  restartSession: vi.fn(),
}));

vi.mock("../api/ws", () => ({
  useSessionWebSocket: (
    _session: string | undefined,
    deliver: (event: ServerMessage) => void,
  ) => {
    socket.deliver = deliver;
    return { send: socket.send };
  },
}));

const key = "02/03/2.3.2";
const part: PartNode = {
  key,
  title: "2.3.2 Cyber Risk",
  kind: "subsection",
  parent: "02/03",
  depth: 2,
  path: "/book/02/03.md",
  leaf: true,
  staleness: "fresh",
  standing: "idle",
  reasons: [],
  questions: 0,
  session: "",
  below: 1,
  outstanding_below: 0,
};
const loop: WorkLoopStatus = {
  work: "work",
  running: false,
  started_at: null,
  finished_at: null,
  passes: [],
  stopped: false,
  failure: "",
};
const tree: WorkTree = {
  id: "work",
  title: "A Work",
  root: "/book",
  target_format: "chapter",
  rooted: true,
  nodes: [part],
  outstanding: 0,
  blocked: 0,
  settled: true,
  loop,
  in_flight: [],
};

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchPipelineStages).mockResolvedValue(["extract", "plan"]);
  vi.mocked(api.fetchWorkTree).mockResolvedValue(tree);
  vi.mocked(api.fetchWorkQuestions).mockResolvedValue([]);
  vi.mocked(api.fetchWorkHistory).mockResolvedValue([]);
  vi.mocked(api.fetchWorkActivity).mockResolvedValue({
    work: "work",
    loop,
    parts: [],
    agents: {},
    working: [],
  });
  vi.mocked(api.startWorkPart).mockResolvedValue({
    ...loop,
    running: true,
    started_at: "2026-08-24T12:00:00Z",
  });
});

function EventCount() {
  return <div>{useSession().state.events.length} received</div>;
}

function showTree() {
  render(
    <MemoryRouter initialEntries={["/work/work"]}>
      <Routes>
        <Route path="/work/:workId" element={<WorkTreeView />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("finding the part to act on", () => {
  it("carries no selection, so acting on a part needs no second control", async () => {
    showTree();

    await screen.findByRole("button", { name: "Run" });
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
  });

  it("puts the tree above what is only read after a part is found", async () => {
    showTree();

    await screen.findByRole("button", { name: "Run" });
    const parts = screen.getByRole("heading", { name: "Parts" });
    const reaches = screen.getByRole("heading", {
      name: "What a change would reach",
    });
    expect(
      parts.compareDocumentPosition(reaches) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe("running one work part", () => {
  it("starts exactly the part whose row was acted on", async () => {
    const user = userEvent.setup();
    showTree();

    const run = await screen.findByRole("button", { name: "Run" });
    vi.mocked(api.fetchWorkTree).mockResolvedValue({
      ...tree,
      loop: { ...loop, running: true },
    });
    await user.click(run);

    await waitFor(() =>
      expect(api.startWorkPart).toHaveBeenCalledWith("work", key, ""),
    );
    expect(
      screen.getByText(
        `Started a run of ${key} — it appears under Being written now`,
      ),
    ).toBeTruthy();
  });

  it("shows the part running before the server has been asked again", async () => {
    const user = userEvent.setup();
    let release: (status: WorkLoopStatus) => void = () => {};
    vi.mocked(api.startWorkPart).mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    showTree();

    await user.click(await screen.findByRole("button", { name: "Run" }));

    // The row has moved on the click, with the request still in flight and no
    // tree fetched since: what used to take a round trip and four refetches.
    expect(screen.getByText("running")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Starting…" })).toBeTruthy();

    await act(async () => {
      release({ ...loop, running: true });
    });
  });

  it("puts a row back as it was when its action is refused", async () => {
    const user = userEvent.setup();
    vi.mocked(api.clearWorkPart).mockRejectedValue(new Error("held by a run"));
    vi.mocked(api.fetchWorkTree).mockResolvedValue({
      ...tree,
      nodes: [{ ...part, standing: "failed" }],
    });
    showTree();

    await user.click(await screen.findByRole("button", { name: "Clear" }));

    await waitFor(() => expect(screen.getByText(/held by a run/)).toBeTruthy());
    expect(screen.getByText("failed")).toBeTruthy();
  });

  it("takes the node the server answered with rather than refetching", async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchWorkTree).mockResolvedValue({
      ...tree,
      nodes: [{ ...part, standing: "failed" }],
    });
    vi.mocked(api.clearWorkPart).mockResolvedValue({ ...part, standing: "idle" });
    showTree();

    await user.click(await screen.findByRole("button", { name: "Clear" }));

    await waitFor(() => expect(api.clearWorkPart).toHaveBeenCalledWith("work", key));
    expect(await screen.findByRole("button", { name: "Run" })).toBeTruthy();
  });
});

describe("session event identity", () => {
  it("does not infer skipped stages from the current stage's position", () => {
    const plan: ServerMessage = {
      type: "stage",
      stage: "plan",
      description: "Planning",
      timestamp: "2026-08-24T02:21:24Z",
    };
    expect(completedStageNames([plan], "running")).toEqual([]);
  });

  it("does not append an event replayed after the REST snapshot", async () => {
    const event: ServerMessage = {
      type: "progress",
      message: "Briefing",
      sequence: 0,
      timestamp: "2026-08-24T02:21:24Z",
    };
    vi.mocked(api.getSession).mockResolvedValue({
      session_id: "run",
      title: "A part",
      status: "running",
      state: {
        doc_id: "",
        doc_url: "",
        title: "A part",
        stage: "brief",
        sections: [],
        pending_questions: [],
        drafted_words: 0,
        target_words: 0,
      },
      cost: {
        total_cost_usd: 0,
        total_input_tokens: 0,
        total_output_tokens: 0,
        duration_s: 0,
        stages: {},
      },
      created_at: "2026-08-24T02:21:23Z",
      profile: null,
      events: [event],
      checkpoints: [],
    });
    render(
      <SessionProvider sessionId="run">
        <EventCount />
      </SessionProvider>,
    );
    expect(await screen.findByText("1 received")).toBeTruthy();
    act(() => socket.deliver?.(event));
    expect(screen.getByText("1 received")).toBeTruthy();
  });
});
