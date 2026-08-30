type FixtureEvent = {
  event_id: string;
  sequence: number;
  timestamp: string;
  kind: string;
  role: string;
  token_estimate: number;
  payload: unknown;
  tool_name?: string;
};

const timestamp = "2026-08-30T06:00:00.000Z";
const sessions = [
  { session_id: "fixture_tools", title: "工具与代码", updated_at: timestamp, event_count: 6 },
  { session_id: "fixture_empty", title: "空任务", updated_at: timestamp, event_count: 0 },
];
const events: Record<string, FixtureEvent[]> = {
  fixture_tools: [
    { event_id: "evt_user", sequence: 1, timestamp, kind: "message", role: "user", token_estimate: 5, payload: "检查 Python 入口并说明结果" },
    { event_id: "evt_reasoning", sequence: 2, timestamp, kind: "assistant_reasoning", role: "assistant", token_estimate: 8, payload: "先读取入口文件，再核对函数结构。" },
    { event_id: "evt_call", sequence: 3, timestamp, kind: "assistant_tool_calls", role: "assistant", token_estimate: 5, payload: { tool_calls: [{ call_id: "call_read", name: "fs_read", arguments: { path: "src/main.py" } }] } },
    { event_id: "evt_result", sequence: 4, timestamp, kind: "tool_result", role: "tool", tool_name: "fs_read", token_estimate: 20, payload: { path: "src/main.py", chars: 49, truncated: false, content: "def main():\n    print('fixture ready')\n\nmain()\n", sha256: "fixture-sha" } },
    { event_id: "evt_answer", sequence: 5, timestamp, kind: "message", role: "assistant", token_estimate: 20, payload: "入口检查完成。\n\n```typescript\nconst ready: boolean = true;\n```" },
    { event_id: "evt_json", sequence: 6, timestamp, kind: "tool_result", role: "tool", tool_name: "context_status", token_estimate: 8, payload: { ok: true, tokens: 66, budget: 1000 } },
  ],
  fixture_empty: [],
};

function contextFor(sessionId: string) {
  const items = events[sessionId] || [];
  return {
    context_version: 1,
    stats: { count: items.length, tokens: items.reduce((total, item) => total + item.token_estimate, 0) },
    working_set: {
      tokens: items.reduce((total, item) => total + item.token_estimate, 0),
      budget: 1000,
      included_event_ids: items.map((item) => item.event_id),
      pinned_event_ids: [], dropped_pinned_ids: [], pinned_tokens: 0,
    },
    pinned_tokens: 0,
  };
}

/** Install deterministic local data for manual browser QA. Never enabled in production. */
export function installInteractionFixture() {
  let activeModelId = "fixture_deepseek";
  let cancelled = false;
  const models = [
    { id: "fixture_deepseek", name: "DeepSeek Fixture", provider: "deepseek", model: "deepseek-chat", base_url: "https://api.deepseek.com", context_window: 64000, hard_limit_ratio: .82, soft_limit_ratio: .7 },
    { id: "fixture_echo", name: "Local Echo", provider: "echo", model: "zagent-local", base_url: "", context_window: 32768, hard_limit_ratio: .82, soft_limit_ratio: .7 },
  ];

  window.zagent = {
    platform: "browser-fixture",
    async request<T>(path: string, options?: { method?: string; body?: unknown }): Promise<T> {
      if (path === "/v1/workspaces") return { workspaces: [{ workspace_id: "fixture_ws", name: "交互测试工作区", path: "/fixture/project", session_count: sessions.length }] } as T;
      if (path === "/v1/config") return { locale: "zh-CN", models, active_model_id: activeModelId, model: models.find((item) => item.id === activeModelId)! } as T;
      if (path.startsWith("/v1/sessions?")) return { sessions } as T;
      if (path === "/v1/sessions" && options?.method === "POST") return sessions[1] as T;
      const eventMatch = path.match(/^\/v1\/sessions\/([^/]+)\/events$/);
      if (eventMatch) return { events: events[eventMatch[1]] || [] } as T;
      const contextMatch = path.match(/^\/v1\/sessions\/([^/]+)\/context$/);
      if (contextMatch) return contextFor(contextMatch[1]) as T;
      if (/^\/v1\/models\/[^/]+\/activate$/.test(path)) {
        activeModelId = path.split("/")[3];
        return { locale: "zh-CN", models, active_model_id: activeModelId, model: models.find((item) => item.id === activeModelId)! } as T;
      }
      if (path === "/v1/extensions") return { extensions: [{ id: "fixture.extension", name: "Fixture Extension", version: "1.0.0", runtime: "declarative", entry: null, contributes: ["tools"], permissions: [], enabled: true, status: "installed", signature_status: "verified", package_sha256: "f".repeat(64) }] } as T;
      if (path === "/v1/mcp/servers") return { servers: [{ name: "fixture-mcp", transport: "http", enabled: true, approved: false, url: "https://fixture.invalid/mcp", status: "approval_required" }] } as T;
      if (path === "/v1/permissions/requests?status=pending") return { requests: [] } as T;
      if (path === "/v1/permissions/grants") return { grants: [] } as T;
      if (path.includes("/context/tools")) return {} as T;
      throw new Error(`fixture does not implement ${options?.method || "GET"} ${path}`);
    },
    async requestStream<T>(path: string, options?: { method?: string; body?: unknown }, onEvent?: (event: StreamEvent) => void) {
      cancelled = false;
      const sessionId = path.split("/")[3];
      const content = String((options?.body as { content?: string })?.content || "");
      const target = events[sessionId] || (events[sessionId] = []);
      target.push({ event_id: `fixture_user_${target.length}`, sequence: target.length + 1, timestamp, kind: "message", role: "user", token_estimate: 3, payload: content });
      for (const part of ["已收到", "本地交互", "测试消息。"] ) {
        await new Promise((resolve) => window.setTimeout(resolve, 25));
        if (cancelled) return { type: "cancelled" };
        onEvent?.({ type: "content", text: part });
      }
      target.push({ event_id: `fixture_answer_${target.length}`, sequence: target.length + 1, timestamp, kind: "message", role: "assistant", token_estimate: 5, payload: "已收到本地交互测试消息。" });
      return { type: "done", result: { stats: { total_tokens: 8, completion_tokens: 5, cache_hit_tokens: 0, cache_miss_tokens: 8, cache_hit_rate: 0, elapsed_seconds: .08, tokens_per_second: 62.5 } } as T };
    },
    async cancelStream() { cancelled = true; return { cancelled: true }; },
  };
}
