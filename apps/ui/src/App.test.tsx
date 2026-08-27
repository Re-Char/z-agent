import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

describe("Z-Agent desktop UI", () => {
  beforeEach(() => {
    window.zagent = {
      platform: "darwin",
      request: vi.fn(async (path: string) => {
        if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws_test", name: "默认工作区", path: "", session_count: 0 }] };
        if (path === "/v1/sessions" || path.startsWith("/v1/sessions?")) return { sessions: [] };
        if (path === "/v1/config") return {
          locale: "zh-CN",
          model: { id: "model_test", name: "", provider: "echo", model: "zagent-local", base_url: "", context_window: 32768, hard_limit_ratio: 0.82, soft_limit_ratio: 0.7 },
          models: [{ id: "model_test", name: "", provider: "echo", model: "zagent-local", base_url: "", context_window: 32768, hard_limit_ratio: 0.82, soft_limit_ratio: 0.7 }],
          active_model_id: "model_test"
        };
        throw new Error(`unexpected path: ${path}`);
      }) as ZAgentBridge["request"]
    };
  });

  it("renders the Chinese-first empty state", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByText("中文长程智能体")).toBeInTheDocument());
    expect(screen.getByText(/记得住过程的智能体/)).toBeInTheDocument();
    expect(screen.getByText("上下文检查器")).toBeInTheDocument();
  });
});

