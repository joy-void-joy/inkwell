import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import * as api from "../api/client";
import type { PartNode, WorkLoopStatus, WorkTree } from "../types";
import { WorkTreeView } from "./WorkTreeView";

vi.mock("../api/client", () => ({
  answerWorkQuestion: vi.fn(),
  clearWorkPart: vi.fn(),
  fetchWhatItReaches: vi.fn(),
  fetchWorkActivity: vi.fn(),
  fetchWorkHistory: vi.fn(),
  fetchWorkQuestions: vi.fn(),
  fetchWorkTree: vi.fn(),
  requestWorkPart: vi.fn(),
  startWorkPart: vi.fn(),
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

describe("running one selected work part", () => {
  it("starts exactly the part selected in the tree", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/work/work"]}>
        <Routes>
          <Route path="/work/:workId" element={<WorkTreeView />} />
        </Routes>
      </MemoryRouter>,
    );

    const selection = await screen.findByRole("radio", {
      name: `Select ${key}: ${part.title}`,
    });
    const run = screen.getByRole("button", {
      name: "Run selected part",
    }) as HTMLButtonElement;
    expect(run.disabled).toBe(true);

    await user.click(selection);
    expect(run.disabled).toBe(false);
    vi.mocked(api.fetchWorkTree).mockResolvedValue({ ...tree, loop: { ...loop, running: true } });
    await user.click(run);

    await waitFor(() =>
      expect(api.startWorkPart).toHaveBeenCalledWith("work", key, ""),
    );
    expect(
      screen.getByText(
        `Started a run of ${key} — it appears under Being written now`,
      ),
    ).toBeTruthy();
    const active = screen.getByRole("button", {
      name: "A work run is active",
    }) as HTMLButtonElement;
    expect(active.disabled).toBe(true);
  });
});
