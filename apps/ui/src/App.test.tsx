import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("renders the Chinese-first empty state", () => {
    render(<App />);
    expect(screen.getByText("中文长程智能体")).toBeInTheDocument();
    expect(screen.getByText("从一个清晰的目标开始")).toBeInTheDocument();
    expect(screen.getByText("敏感文件隔离")).toBeInTheDocument();
    expect(screen.getByText("上下文检查器")).toBeInTheDocument();
  });

  it("keeps model reasoning collapsed until the user explicitly expands it", async () => {
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws_test", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return {
        locale: "zh-CN", model: { id: "m", name: "", provider: "deepseek", model: "deepseek-v4-flash", base_url: "https://api.deepseek.com", context_window: 1000000, hard_limit_ratio: .82, soft_limit_ratio: .7 },
        models: [], active_model_id: "m"
      };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "工具任务", updated_at: new Date().toISOString(), event_count: 2 }] };
      if (path === "/v1/sessions/s1/events") return { events: [{
        event_id: "evt_tool", sequence: 1, timestamp: new Date().toISOString(), kind: "assistant_tool_calls", role: "assistant", token_estimate: 12,
        payload: { reasoning_content: "不应出现在界面中的内部思考", tool_calls: [{ call_id: "c1", name: "fs_read", arguments: { path: "README.md" } }] }
      }] };
      if (path === "/v1/sessions/s1/context") return { stats: { count: 1, tokens: 12 }, working_set: { tokens: 12, budget: 1000, included_event_ids: ["evt_tool"], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    await waitFor(() => expect(screen.getByRole("button", { name: "显示工具记录" })).toHaveTextContent("工具记录 1"));
    const reasoning = screen.getByRole("button", { name: /思考过程/ });
    expect(reasoning).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/README\.md/)).not.toBeInTheDocument();
    expect(screen.queryByText(/内部思考/)).not.toBeInTheDocument();
    fireEvent.click(reasoning);
    expect(reasoning).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/内部思考/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "显示工具记录" }));
    expect(screen.getByText(/README\.md/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("先连接项目目录")).not.toBeInTheDocument());
  });

  it("shows archive compaction metrics in the context inspector", async () => {
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "归档任务", updated_at: new Date().toISOString(), event_count: 4 }] };
      if (path === "/v1/sessions/s1/events") return { events: [] };
      if (path === "/v1/sessions/s1/context") return {
        stats: { count: 4, tokens: 320 },
        working_set: { tokens: 40, budget: 1000, included_event_ids: [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 },
        latest_archive: { archive_id: "arc_test", start_sequence: 1, end_sequence: 3, state: { goal: "完成阶段" } },
        archive_stats: { count: 3, tokens: 280 }, pinned_tokens: 0,
      };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    await waitFor(() => expect(screen.getByText("arc_test")).toBeInTheDocument());
    expect(screen.getByText(/已外置 3 个事件（约 280 tokens）/)).toBeInTheDocument();
    expect(screen.getByText(/原文仍可检索和按 event ID 取回/)).toBeInTheDocument();
  });

  it("lets the user stop an active stream without showing an error", async () => {
    let finishStream: ((value: { type: string }) => void) | undefined;
    let cancelled = false;
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "任务", updated_at: new Date().toISOString(), event_count: 0 }] };
      if (path === "/v1/sessions/s1/events") return { events: cancelled ? [
        { event_id: "u1", sequence: 1, timestamp: new Date().toISOString(), kind: "message", role: "user", token_estimate: 2, payload: "分析代码" },
        { event_id: "t1", sequence: 2, timestamp: new Date().toISOString(), kind: "assistant_tool_calls", role: "assistant", token_estimate: 8, payload: { reasoning_content: "停止后也不能泄露的思考", tool_calls: [{ call_id: "c1", name: "fs_read", arguments: { path: "README.md" } }] } },
        { event_id: "r1", sequence: 3, timestamp: new Date().toISOString(), kind: "tool_result", role: "tool", tool_name: "fs_read", token_estimate: 8, payload: { content: "内部工具结果" } },
      ] : [] };
      if (path === "/v1/sessions/s1/context") return { stats: { count: cancelled ? 3 : 0, tokens: 0 }, working_set: { tokens: 0, budget: 1000, included_event_ids: cancelled ? ["u1", "t1", "r1"] : [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];
    window.zagent.requestStream = vi.fn(() => new Promise<{ type: string }>((resolve) => { finishStream = resolve; })) as ZAgentBridge["requestStream"];
    window.zagent.cancelStream = vi.fn(async () => {
      cancelled = true;
      finishStream?.({ type: "cancelled" });
      return { cancelled: true };
    });

    render(<App />);
    await waitFor(() => expect(screen.getAllByText("任务").length).toBeGreaterThan(0));
    const input = await screen.findByRole("textbox", { name: "任务输入" });
    fireEvent.change(input, { target: { value: "分析代码" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const stop = await screen.findByRole("button", { name: "停止生成" });
    fireEvent.click(stop);
    expect(window.zagent.cancelStream).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("button", { name: "发送消息" })).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "显示工具记录" })).toHaveTextContent("工具记录 2");
    expect(screen.getByRole("button", { name: /思考过程/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/停止后也不能泄露/)).not.toBeInTheDocument();
    expect(screen.queryByText(/README\.md/)).not.toBeInTheDocument();
    expect(screen.queryByText(/内部工具结果/)).not.toBeInTheDocument();
  });

  it("restores the draft and shows a useful error when session creation fails", async () => {
    window.zagent.request = vi.fn(async (path: string, options) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 0 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?") || (path === "/v1/sessions" && !options?.method)) return { sessions: [] };
      if (path === "/v1/sessions" && options?.method === "POST") throw new Error("工作区不可用");
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    const input = await screen.findByRole("textbox", { name: "任务输入" });
    fireEvent.change(input, { target: { value: "请检查代码" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("工作区不可用"));
    expect(input).toHaveValue("请检查代码");
    expect(screen.getByRole("button", { name: "发送消息" })).toBeEnabled();
  });
});
