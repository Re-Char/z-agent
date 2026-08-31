import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import "./styles.css";

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

  it("shows Core recovery and offline status reported by Electron", async () => {
    let reportStatus: ((status: { status: "online" | "recovering" | "offline"; attempt?: number }) => void) | undefined;
    window.zagent.onCoreStatus = vi.fn((callback) => { reportStatus = callback; });

    render(<App />);
    await waitFor(() => expect(window.zagent.onCoreStatus).toHaveBeenCalledOnce());
    reportStatus?.({ status: "recovering", attempt: 1 });
    expect(await screen.findByText("核心恢复中")).toBeInTheDocument();
    reportStatus?.({ status: "offline" });
    expect(await screen.findByText("核心离线")).toBeInTheDocument();
  });

  it("keeps the context inspector inert until the user opens it", () => {
    const { container } = render(<App />);
    const inspector = container.querySelector("aside.inspector");
    expect(inspector).not.toBeNull();
    expect(inspector).toHaveAttribute("aria-hidden", "true");
    expect(inspector).toHaveAttribute("inert");

    fireEvent.click(screen.getByRole("button", { name: "切换上下文检查器" }));
    expect(inspector).toHaveAttribute("aria-hidden", "false");
    expect(inspector).not.toHaveAttribute("inert");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(inspector).toHaveAttribute("aria-hidden", "true");
    expect(inspector).toHaveAttribute("inert");
  });

  it("renders highlighted JSON arguments and highlighted file contents in tool records", async () => {
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "代码检查", updated_at: new Date().toISOString(), event_count: 2 }] };
      if (path === "/v1/sessions/s1/events") return { events: [
        { event_id: "call1", sequence: 1, timestamp: new Date().toISOString(), kind: "assistant_tool_calls", role: "assistant", token_estimate: 5, payload: { tool_calls: [{ call_id: "c1", name: "fs_read", arguments: { path: "src/main.py" } }] } },
        { event_id: "result1", sequence: 2, timestamp: new Date().toISOString(), kind: "tool_result", role: "tool", tool_name: "fs_read", token_estimate: 8, payload: { path: "src/main.py", content: "def main():\n    return True\n", truncated: false, sha256: "abc" } },
      ] };
      if (path === "/v1/sessions/s1/context") return { stats: { count: 2, tokens: 13 }, working_set: { tokens: 13, budget: 1000, included_event_ids: ["call1", "result1"], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    const { container } = render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "显示工具记录" }));
    const disclosures = await screen.findAllByRole("button", { name: /fs_read/ });
    disclosures.forEach((button) => fireEvent.click(button));

    expect(container.querySelector("code.language-json .hljs-attr")).toHaveTextContent('"path"');
    expect(container.querySelector("code.language-python .hljs-keyword")).toHaveTextContent("def");
    expect(screen.getByRole("button", { name: "复制 src/main.py 代码" })).toBeInTheDocument();
    const codePre = container.querySelector<HTMLElement>(".code-block pre");
    expect(codePre).not.toBeNull();
    expect(getComputedStyle(codePre!).whiteSpace).toBe("pre");

    fireEvent.click(screen.getByRole("button", { name: "隐藏工具记录" }));
    fireEvent.click(screen.getByRole("button", { name: "显示工具记录" }));
    (await screen.findAllByRole("button", { name: /fs_read/ })).forEach((button) => fireEvent.click(button));
    expect(container.querySelector("code.language-python .hljs-keyword")).toHaveTextContent("def");
  });

  it("shows bounded tool progress while a large file call is being prepared", async () => {
    window.zagent.request = vi.fn(async (path: string, options) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "deepseek", model: "deepseek-chat", base_url: "https://api.deepseek.com", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "2048", updated_at: new Date().toISOString(), event_count: 0 }] };
      if (path === "/v1/sessions/s1/events") return { events: [] };
      if (path === "/v1/sessions/s1/context") return { context_version: 0, stats: { count: 0, tokens: 0 }, working_set: { tokens: 0, budget: 1000, included_event_ids: [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected ${options?.method || "GET"} ${path}`);
    }) as ZAgentBridge["request"];
    let finish: ((value: { type: string; result?: unknown }) => void) | undefined;
    window.zagent.requestStream = vi.fn((_path, _options, onEvent) => new Promise((resolve) => {
      onEvent?.({ type: "status", round: 1, message: "第 1 轮：正在请求模型" });
      onEvent?.({ type: "tool_call", name: "fs_write" });
      onEvent?.({ type: "tool_result", name: "fs_write", ok: true, detail: "index.html" });
      onEvent?.({ type: "content", text: "已完成 2048" });
      finish = resolve;
    })) as ZAgentBridge["requestStream"];

    const { container } = render(<App />);
    await waitFor(() => expect(screen.getAllByText("2048").length).toBeGreaterThan(0));
    const input = screen.getByRole("textbox", { name: "任务输入" });
    fireEvent.change(input, { target: { value: "生成 2048 小游戏" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect((await screen.findAllByText("正在准备工具：fs_write")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("fs_write已完成，正在继续 · index.html")).length).toBeGreaterThan(0);
    const progress = container.querySelector(".thinking.task-running");
    const streamingReply = container.querySelector(".streaming-bubble");
    expect(progress).not.toBeNull();
    expect(streamingReply).not.toBeNull();
    expect(progress!.compareDocumentPosition(streamingReply!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    finish?.({ type: "done", result: { stats: {
      total_tokens: 8, completion_tokens: 4, cache_hit_tokens: 0, cache_miss_tokens: 8,
      cache_hit_rate: 0, elapsed_seconds: 1, tokens_per_second: 4,
    } } });
    await waitFor(() => expect(screen.getByRole("button", { name: "发送消息" })).toBeInTheDocument());
  });

  it("opens an immediate approval dialog and resumes the running test", async () => {
    let streamCallback: ((event: StreamEvent) => void) | undefined;
    let finishStream: ((value: { type: string; result?: unknown }) => void) | undefined;
    const permission = {
      request_id: "prm_runner_1", session_id: "s1", subject_type: "runner",
      subject_id: "python_unittest", action: "execute",
      details: { command_template: ["python", "-m", "unittest", "discover"], network: false },
      status: "pending", created_at: new Date().toISOString(),
    };
    window.zagent.request = vi.fn(async (path: string, options) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "Python 项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "deepseek", model: "deepseek-chat", base_url: "https://api.deepseek.com", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "Python 2048", updated_at: new Date().toISOString(), event_count: 0 }] };
      if (path === "/v1/sessions/s1/events") return { events: [] };
      if (path === "/v1/sessions/s1/context") return { context_version: 0, stats: { count: 0, tokens: 0 }, working_set: { tokens: 0, budget: 1000, included_event_ids: [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      if (path === "/v1/permissions/requests/prm_runner_1/decision") {
        expect(options?.method).toBe("POST");
        expect(options?.body).toEqual({ decision: "approved", scope: "once" });
        streamCallback?.({ type: "tool_result", name: "runner_execute", ok: true });
        finishStream?.({ type: "done", result: { stats: { total_tokens: 3, completion_tokens: 1, cache_hit_tokens: 0, cache_miss_tokens: 3, cache_hit_rate: 0, elapsed_seconds: 1, tokens_per_second: 1 } } });
        return { request: { ...permission, status: "approved" } };
      }
      throw new Error(`unexpected ${options?.method || "GET"} ${path}`);
    }) as ZAgentBridge["request"];
    window.zagent.requestStream = vi.fn((_path, _options, onEvent) => new Promise((resolve) => {
      streamCallback = onEvent;
      finishStream = resolve;
      onEvent?.({ type: "status", message: "第 2 轮：正在请求模型" });
      onEvent?.({ type: "permission_required", request: permission });
    })) as ZAgentBridge["requestStream"];

    render(<App />);
    await screen.findByRole("button", { name: /Python 2048/ });
    const input = screen.getByRole("textbox", { name: "任务输入" });
    fireEvent.change(input, { target: { value: "运行 Python 测试" } });
    fireEvent.keyDown(input, { key: "Enter" });

    const dialog = await screen.findByRole("alertdialog", { name: "需要批准" });
    expect(dialog).toHaveTextContent("运行测试 python_unittest");
    expect(dialog).toHaveTextContent("python -m unittest discover");
    expect(dialog).toHaveTextContent("去敏只读快照");
    fireEvent.click(screen.getByRole("button", { name: "仅此一次" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog", { name: "需要批准" })).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "发送消息" })).toBeInTheDocument());
  });

  it("keeps running progress on its session and hides it after switching", async () => {
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 2 }] };
      if (path === "/v1/config") return { locale: "zh-CN", recent_event_limit: 96, model: { id: "m", name: "", provider: "deepseek", model: "deepseek-chat", base_url: "https://api.deepseek.com", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [
        { session_id: "s1", title: "2048 任务", updated_at: new Date().toISOString(), event_count: 0 },
        { session_id: "s2", title: "其他对话", updated_at: new Date().toISOString(), event_count: 0 },
      ] };
      if (/\/v1\/sessions\/s[12]\/events/.test(path)) return { events: [] };
      if (/\/v1\/sessions\/s[12]\/context/.test(path)) return { context_version: 0, stats: { count: 0, tokens: 0 }, working_set: { tokens: 0, budget: 1000, included_event_ids: [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, memory_stats: { active: 0, candidates: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];
    let finish: ((value: { type: string; result?: unknown }) => void) | undefined;
    window.zagent.requestStream = vi.fn((_path, _options, onEvent) => new Promise((resolve) => {
      onEvent?.({ type: "status", round: 1, message: "第 1 轮：正在请求模型" });
      onEvent?.({ type: "tool_call", name: "fs_write" });
      finish = resolve;
    })) as ZAgentBridge["requestStream"];

    render(<App />);
    await screen.findByRole("button", { name: /2048 任务/ });
    const input = screen.getByRole("textbox", { name: "任务输入" });
    fireEvent.change(input, { target: { value: "创建 2048" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByLabelText("正在执行")).toBeInTheDocument();
    expect((await screen.findAllByText("正在准备工具：fs_write")).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /其他对话/ }));
    await waitFor(() => expect(document.querySelector(".thinking.task-running")).toBeNull());
    expect(screen.queryByText("第 1 轮：正在请求模型", { selector: ".thinking span" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "任务输入" })).toBeDisabled();
    expect(screen.getByPlaceholderText("另一对话正在执行任务；可点击左侧加载项返回查看")).toBeInTheDocument();
    expect(screen.getByLabelText("正在执行").closest("button")).toHaveTextContent("2048 任务");

    fireEvent.click(screen.getByRole("button", { name: /2048 任务/ }));
    expect((await screen.findAllByText("正在准备工具：fs_write")).length).toBeGreaterThan(0);
    finish?.({ type: "done", result: { stats: { total_tokens: 1, completion_tokens: 1, cache_hit_tokens: 0, cache_miss_tokens: 1, cache_hit_rate: 0, elapsed_seconds: 1, tokens_per_second: 1 } } });
    await waitFor(() => expect(screen.queryByLabelText("正在执行")).not.toBeInTheDocument());
  });

  it("explains pinned evidence separately from the recent working-set preview", async () => {
    const pinnedId = "evt_pinned_older_than_preview";
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", recent_event_limit: 96, model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 64000, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "上下文测试", updated_at: new Date().toISOString(), event_count: 1 }] };
      if (path === "/v1/sessions/s1/events") return { events: [{ event_id: pinnedId, sequence: 1, timestamp: new Date().toISOString(), kind: "message", role: "user", token_estimate: 5, payload: "必须一直保留的架构决策" }] };
      if (path === "/v1/sessions/s1/context") return { stats: { count: 20, tokens: 200 }, working_set: { tokens: 200, budget: 52480, included_event_ids: [pinnedId, ...Array.from({ length: 12 }, (_, index) => `recent_${index}`)], pinned_event_ids: [pinnedId], dropped_pinned_ids: [], pinned_tokens: 5 }, memory_stats: { active: 0, candidates: 1 }, pinned_tokens: 5 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    expect(await screen.findByRole("button", { name: "长期记忆 · 1 待确认" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "切换上下文检查器" }));
    expect(screen.getByText("固定证据（持续保留）")).toBeInTheDocument();
    expect(screen.getAllByText("必须一直保留的架构决策").length).toBeGreaterThan(1);
    expect(screen.getByText(/模型本轮实际收到 13 个事件/)).toBeInTheDocument();
    expect(screen.getByText(/预算是安全上限，不是填充目标/)).toBeInTheDocument();
  });

  it("clears stale conversation content immediately while switching sessions", async () => {
    let resolveSecond: ((value: { events: unknown[] }) => void) | undefined;
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 2 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [
        { session_id: "s1", title: "旧会话", updated_at: new Date().toISOString(), event_count: 1 },
        { session_id: "s2", title: "新会话", updated_at: new Date().toISOString(), event_count: 1 },
      ] };
      if (path === "/v1/sessions/s1/events") return { events: [{ event_id: "old", sequence: 1, timestamp: new Date().toISOString(), kind: "message", role: "assistant", token_estimate: 2, payload: "只属于旧会话" }] };
      if (path === "/v1/sessions/s1/context") return { stats: { count: 1, tokens: 2 }, working_set: { tokens: 2, budget: 1000, included_event_ids: ["old"], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      if (path === "/v1/sessions/s2/events") return await new Promise<{ events: unknown[] }>((resolve) => { resolveSecond = resolve; });
      if (path === "/v1/sessions/s2/context") return { stats: { count: 1, tokens: 2 }, working_set: { tokens: 2, budget: 1000, included_event_ids: ["new"], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    expect((await screen.findAllByText("只属于旧会话")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /新会话/ }));
    expect(screen.queryAllByText("只属于旧会话")).toHaveLength(0);
    resolveSecond?.({ events: [{ event_id: "new", sequence: 1, timestamp: new Date().toISOString(), kind: "message", role: "assistant", token_estimate: 2, payload: "只属于新会话" }] });
    expect((await screen.findAllByText("只属于新会话")).length).toBeGreaterThan(0);
  });

  it("shows an explicit empty working-set state and closes modals with Escape", async () => {
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "空会话", updated_at: new Date().toISOString(), event_count: 0 }] };
      if (path === "/v1/sessions/s1/events") return { events: [] };
      if (path === "/v1/sessions/s1/context") return { stats: { count: 0, tokens: 0 }, working_set: { tokens: 0, budget: 1000, included_event_ids: [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    await waitFor(() => expect(screen.getByText("暂无普通事件")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "模型设置" }));
    expect(screen.getByRole("dialog", { name: "模型设置" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "模型设置" })).not.toBeInTheDocument();
  });

  it("opens a clean landing page after creating a workspace from an active conversation", async () => {
    let created = false;
    window.zagent.request = vi.fn(async (path: string, options) => {
      if (path === "/v1/workspaces" && options?.method === "POST") {
        created = true;
        return { workspace: { workspace_id: "ws_new", name: "新项目", path: "/tmp/new", session_count: 0 } };
      }
      if (path === "/v1/workspaces") return { workspaces: [
        { workspace_id: "ws_old", name: "旧项目", path: "/tmp/old", session_count: 1 },
        ...(created ? [{ workspace_id: "ws_new", name: "新项目", path: "/tmp/new", session_count: 0 }] : []),
      ] };
      if (path === "/v1/config") return {
        locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m"
      };
      if (path === "/v1/sessions?workspace_id=ws_old") return { sessions: [{ session_id: "s_old", title: "旧任务", updated_at: new Date().toISOString(), event_count: 1 }] };
      if (path === "/v1/sessions?workspace_id=ws_new") return { sessions: [] };
      if (path === "/v1/sessions/s_old/events") return { events: [{
        event_id: "evt_old", sequence: 1, timestamp: new Date().toISOString(), kind: "message", role: "assistant", token_estimate: 2, payload: "旧工作区内容"
      }] };
      if (path === "/v1/sessions/s_old/context") return { stats: { count: 1, tokens: 2 }, working_set: { tokens: 2, budget: 1000, included_event_ids: ["evt_old"], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    await waitFor(() => expect(screen.getAllByText("旧工作区内容").length).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole("button", { name: "新建工作区" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "新项目" } });
    fireEvent.change(screen.getByLabelText(/路径（agent 可访问的项目目录）/), { target: { value: "/tmp/new" } });
    fireEvent.click(screen.getByRole("button", { name: "创建工作区" }));

    await waitFor(() => expect(screen.getByRole("combobox", { name: "切换工作区" })).toHaveValue("ws_new"));
    expect(screen.queryAllByText("旧工作区内容")).toHaveLength(0);
    expect(screen.getByText("从一个清晰的目标开始")).toBeInTheDocument();
    expect(screen.getByText("开始一个新任务")).toBeInTheDocument();
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

  it("offers one-click continuation from a persisted checkpoint", async () => {
    let checkpointActive = false;
    let streamCalls = 0;
    const checkpoint = {
      checkpoint_id: "chk_1234567890ab", reason: "max_tool_rounds",
      state: { status: "paused", completed: [{ tool: "fs_read" }], pending: [{ tool: "fs_write" }] },
    };
    window.zagent.request = vi.fn(async (path: string) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "长任务", updated_at: new Date().toISOString(), event_count: checkpointActive ? 2 : 0 }] };
      if (path === "/v1/sessions/s1/events") return { events: checkpointActive ? [
        { event_id: "u1", sequence: 1, timestamp: new Date().toISOString(), kind: "message", role: "user", token_estimate: 2, payload: "完成项目" },
        { event_id: "cp1", sequence: 2, timestamp: new Date().toISOString(), kind: "checkpoint", role: "system", token_estimate: 8, payload: checkpoint },
      ] : [] };
      if (path === "/v1/sessions/s1/context") return {
        context_version: 4,
        stats: { count: checkpointActive ? 2 : 0, tokens: 0 },
        working_set: { tokens: 0, budget: 1000, included_event_ids: [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 },
        latest_checkpoint: checkpointActive ? checkpoint : null, pinned_tokens: 0,
      };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];
    window.zagent.requestStream = vi.fn(async () => {
      streamCalls += 1;
      if (streamCalls === 1) {
        checkpointActive = true;
        return { type: "error", message: "已达到本轮工具调用上限", checkpoint };
      }
      checkpointActive = false;
      return { type: "done", result: { stats: { total_tokens: 1, completion_tokens: 1, cache_hit_tokens: 0, cache_miss_tokens: 0, cache_hit_rate: 0, elapsed_seconds: 1, tokens_per_second: 1 } } };
    }) as ZAgentBridge["requestStream"];

    render(<App />);
    await waitFor(() => expect(screen.getAllByText("长任务").length).toBeGreaterThan(0));
    const input = await screen.findByRole("textbox", { name: "任务输入" });
    fireEvent.change(input, { target: { value: "完成项目" } });
    fireEvent.keyDown(input, { key: "Enter" });

    const resume = await screen.findByRole("button", { name: "继续任务" });
    expect((vi.mocked(window.zagent.requestStream!).mock.calls[0][1]?.body as { expected_context_version: number }).expected_context_version).toBe(4);
    expect(screen.getByText(/进度已写入 1234567890ab/)).toBeInTheDocument();
    expect(screen.queryByText(/kind.*checkpoint/)).not.toBeInTheDocument();
    fireEvent.click(resume);

    await waitFor(() => expect(window.zagent.requestStream).toHaveBeenCalledTimes(2));
    const secondOptions = vi.mocked(window.zagent.requestStream!).mock.calls[1][1];
    expect((secondOptions?.body as { content: string }).content).toContain("文件 SHA");
    await waitFor(() => expect(screen.queryByRole("button", { name: "继续任务" })).not.toBeInTheDocument());
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

  it("reviews, searches, confirms conflicting, and deletes long-term memories", async () => {
    let confirmed = false;
    let deleted = false;
    let pinned = false;
    let correctedContent = "项目改用 SQLite";
    let resolvePin: ((value: { memory: Record<string, unknown> }) => void) | undefined;
    const active = {
      memory_id: "mem_active_12345678", scope_type: "workspace", scope_id: "ws", memory_type: "semantic",
      memory_key: "项目数据库", content: "项目使用 PostgreSQL", confidence: .9, status: "active", pinned: false,
      created_reason: "用户确认", source_session_id: "s1", source_event_ids: ["evt1"],
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(), last_verified_at: new Date().toISOString(),
    };
    const candidate = {
      ...active, memory_id: "mem_candidate_87654321", content: "项目改用 SQLite", confidence: .82,
      status: "candidate", conflict_memory_id: active.memory_id, source_event_ids: ["evt2"],
    };
    window.zagent.request = vi.fn(async (path: string, options) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 1 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [{ session_id: "s1", title: "记忆任务", updated_at: new Date().toISOString(), event_count: 0 }] };
      if (path === "/v1/sessions/s1/events") return { events: [] };
      if (path === "/v1/sessions/s1/context") return { stats: { count: 0, tokens: 0 }, working_set: { tokens: 0, budget: 1000, included_event_ids: [], pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0 }, pinned_tokens: 0 };
      if (path === "/v1/sessions/s1/memories?include_candidates=true&limit=100") {
        return { memories: deleted ? [] : confirmed ? [{ ...candidate, content: correctedContent, status: "active", pinned, conflict_memory_id: null }] : [candidate, active] };
      }
      if (path === "/v1/sessions/s1/memories/export") return {
        schema_version: 1, exported_at: new Date().toISOString(), session_id: "s1", workspace_id: "ws", memories: [],
      };
      if (path === "/v1/sessions/s1/memories/mem_candidate_87654321/confirm" && options?.method === "POST") {
        expect(options.body).toEqual({ supersedes_memory_id: "mem_active_12345678" });
        confirmed = true;
        return { memory: { ...candidate, status: "active" } };
      }
      if (path === "/v1/sessions/s1/memories/mem_candidate_87654321/audit?limit=50") return { audit: [{
        audit_id: "maud_1", memory_id: candidate.memory_id, action: "created", content_sha256: "a".repeat(64),
        details: { status: "candidate" }, created_at: new Date().toISOString(),
      }] };
      if (path === "/v1/sessions/s1/memories/mem_candidate_87654321" && options?.method === "DELETE") {
        expect(options.body).toEqual({ reason: "用户在长期记忆管理界面中删除" });
        deleted = true;
        return { tombstone: true };
      }
      if (path === "/v1/sessions/s1/memories/mem_candidate_87654321" && options?.method === "PATCH") {
        expect(options.body).toEqual({ pinned: true, expected_pinned: false });
        return await new Promise<{ memory: Record<string, unknown> }>((resolve) => { resolvePin = resolve; });
      }
      if (path === "/v1/sessions/s1/memories/mem_candidate_87654321/correct" && options?.method === "POST") {
        expect(options.body).toEqual({ content: "项目改用 SQLite 并启用 WAL", reason: "用户在长期记忆管理界面中纠正" });
        correctedContent = "项目改用 SQLite 并启用 WAL";
        return { memory: { ...candidate, content: correctedContent, status: "active" }, superseded_memory_id: candidate.memory_id, evidence_event_id: "evt_fix" };
      }
      if (path === "/v1/sessions/s1/memories?query=SQLite&limit=20") return { results: [{
        memory: { ...candidate, status: "active", conflict_memory_id: null }, channels: ["lexical", "sparse"],
        exact_match: true, fusion_score: .03, sparse_query_coverage: 1, matched_terms: ["sqlite"],
      }] };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];
    window.zagent.saveJson = vi.fn(async () => "/tmp/zagent-memories.json");

    render(<App />);
    const memoryButton = await screen.findByRole("button", { name: "长期记忆" });
    await waitFor(() => expect(memoryButton).toBeEnabled());
    fireEvent.click(memoryButton);
    expect(await screen.findByText("待确认", { selector: ".memory-badges span" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "导出 JSON" }));
    expect(await screen.findByRole("status")).toHaveTextContent("/tmp/zagent-memories.json");
    expect(window.zagent.saveJson).toHaveBeenCalledWith(
      expect.stringMatching(/^zagent-memories-\d{4}-\d{2}-\d{2}\.json$/),
      expect.stringContaining('"schema_version": 1'),
    );
    expect(screen.getByText(/确认后将替换同名旧记忆/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "查看审计" })[0]);
    expect(await screen.findByLabelText("项目数据库 审计记录")).toHaveTextContent("created");
    fireEvent.click(screen.getByRole("button", { name: "确认并替换" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "确认并替换" })).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "固定" }));
    await waitFor(() => expect(resolvePin).toBeDefined());
    expect(screen.getByRole("textbox", { name: "搜索长期记忆" })).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "删除" })[0]).toBeDisabled();
    pinned = true;
    resolvePin?.({ memory: { ...candidate, status: "active", pinned: true } });
    await waitFor(() => expect(screen.getByRole("button", { name: "取消固定" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "纠正" }));
    const correction = screen.getByRole("textbox", { name: "纠正后的记忆正文" });
    fireEvent.change(correction, { target: { value: "项目改用 SQLite 并启用 WAL" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并替换" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "纠正长期记忆" })).not.toBeInTheDocument());
    expect(screen.getByText("项目改用 SQLite 并启用 WAL", { selector: ".memory-content" })).toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "搜索长期记忆" }), { target: { value: "SQLite" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    expect(await screen.findByText(/召回：精确命中.*匹配 sqlite.*覆盖 100%/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(screen.getByRole("dialog", { name: "确认操作" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "确认操作" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "长期记忆" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(screen.getByText("尚未形成长期记忆")).toBeInTheDocument());
  });

  it("imports an extension package and tests a real MCP connection from the desktop modal", async () => {
    let imported = false;
    let approved = false;
    window.zagent.selectExtension = vi.fn(async () => "/tmp/real-extension.zip");
    window.zagent.request = vi.fn(async (path: string, options) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 0 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [] };
      if (path === "/v1/extensions" && !options?.method) return { extensions: imported ? [{
        id: "com.example.real", name: "真实扩展", version: "2.0.0", runtime: "declarative", entry: null,
        contributes: ["skills"], permissions: [], enabled: false, status: "installed", package_sha256: "a".repeat(64),
      }] : [] };
      if (path === "/v1/extensions/import" && options?.method === "POST") {
        expect(options.body).toEqual({ source_path: "/tmp/real-extension.zip", enabled: false });
        imported = true;
        return { extension: {} };
      }
      if (path === "/v1/mcp/servers" && !options?.method) return { servers: [{
        name: "echo", transport: "stdio", enabled: true, approved, command: "python", args: ["server.py"],
        status: approved ? "connected" : "approval_required",
      }] };
      if (path === "/v1/mcp/servers/echo" && options?.method === "PATCH") {
        approved = true;
        return { server: {} };
      }
      if (path === "/v1/mcp/servers/echo/connect" && options?.method === "POST") return { connected: true };
      if (path === "/v1/mcp/servers/echo/tools") return { tools: [{ name: "echo", description: "Echo", inputSchema: { type: "object" } }] };
      if (path === "/v1/permissions/requests?status=pending") return { requests: [] };
      if (path === "/v1/permissions/grants") return { grants: [] };
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "扩展与 MCP" }));
    fireEvent.click(await screen.findByRole("button", { name: "选择…" }));
    await waitFor(() => expect(screen.getByLabelText("扩展根目录或 ZIP")).toHaveValue("/tmp/real-extension.zip"));
    fireEvent.click(screen.getByRole("button", { name: "安全导入" }));
    await waitFor(() => expect(screen.getByText("真实扩展")).toBeInTheDocument());
    expect(screen.getByText(/SHA aaaaaaaaaaaa/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "连接并读取工具" }));
    await waitFor(() => expect(screen.getByText("已发现 1 个工具")).toBeInTheDocument());
    expect(screen.getByText("echo", { selector: "code" })).toBeInTheDocument();
  });

  it("reviews per-action permissions and imports a registry remote without auto-approval", async () => {
    let decided = false;
    let imported = false;
    window.zagent.request = vi.fn(async (path: string, options) => {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "ws", name: "项目", path: "/tmp/project", session_count: 0 }] };
      if (path === "/v1/config") return { locale: "zh-CN", model: { id: "m", name: "", provider: "echo", model: "local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 }, models: [], active_model_id: "m" };
      if (path.startsWith("/v1/sessions?")) return { sessions: [] };
      if (path === "/v1/extensions") return { extensions: [] };
      if (path === "/v1/mcp/servers") return { servers: imported ? [{ name: "registry-echo", transport: "http", enabled: true, approved: false, url: "https://mcp.example/mcp", status: "approval_required" }] : [] };
      if (path === "/v1/permissions/requests?status=pending") return { requests: decided ? [] : [{ request_id: "prm_1", subject_type: "extension", subject_id: "com.example.echo", action: "tool:write", details: { path: "README.md" }, status: "pending", created_at: new Date().toISOString() }] };
      if (path === "/v1/permissions/grants") return { grants: [] };
      if (path === "/v1/permissions/requests/prm_1/decision" && options?.method === "POST") {
        expect(options.body).toEqual({ decision: "approved", scope: "once" });
        decided = true;
        return { request: { status: "approved" } };
      }
      if (path.startsWith("/v1/mcp/registry/servers?")) return { servers: [{ server: { name: "io.example/echo", description: "Echo MCP", version: "1.0.0" } }] };
      if (path === "/v1/mcp/registry/import" && options?.method === "POST") {
        expect(options.body).toEqual({ server_name: "io.example/echo", version: "1.0.0" });
        imported = true;
        return { server: { approved: false } };
      }
      throw new Error(`unexpected path: ${path}`);
    }) as ZAgentBridge["request"];

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "扩展与 MCP" }));
    fireEvent.click(await screen.findByRole("button", { name: "仅这一次" }));
    await waitFor(() => expect(screen.getByText(/当前没有待处理授权/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("搜索 MCP Registry"), { target: { value: "echo" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => expect(screen.getByText("io.example/echo")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "导入远程端点" }));
    await waitFor(() => expect(screen.getByText("registry-echo")).toBeInTheDocument());
    expect(screen.getByText(/approval_required/)).toBeInTheDocument();
  });
});
