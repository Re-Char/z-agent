import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CodeBlock, languageFromPath, Markdown } from "./Markdown";

type Session = { session_id: string; title: string; updated_at: string; event_count: number };
type Event = { event_id: string; sequence: number; timestamp: string; kind: string; role: string; payload: unknown; token_estimate: number; tool_name?: string };
type Checkpoint = { checkpoint_id: string; reason: string; state: { status?: string; objective?: string; completed?: unknown[]; pending?: unknown[]; failure_reason?: string } };
type ContextStatus = { context_version?: number; stats: { count: number; tokens: number }; working_set: { tokens: number; budget: number; included_event_ids: string[]; pinned_event_ids: string[]; dropped_pinned_ids: string[]; pinned_tokens: number }; latest_archive?: { archive_id: string; start_sequence: number; end_sequence: number; state: unknown }; latest_checkpoint?: Checkpoint | null; archive_stats?: { count: number; tokens: number }; memory_stats?: { active: number; candidates: number }; warning?: string | null; pinned_tokens: number };
type ModelConfig = { id: string; name: string; provider: string; model: string; base_url: string; context_window: number; soft_limit_ratio: number; hard_limit_ratio: number };
type AppConfig = { locale: string; model: ModelConfig; models: ModelConfig[]; active_model_id: string; recent_event_limit?: number };
type TokenStats = { total_tokens: number; completion_tokens: number; cache_hit_tokens: number; cache_miss_tokens: number; cache_hit_rate: number; elapsed_seconds: number; tokens_per_second: number };
type McpServer = { name: string; transport: string; enabled: boolean; approved: boolean; command?: string | null; args?: string[] | null; cwd?: string | null; env?: string[] | null; url?: string | null; status: string; oauth?: boolean; oauth_client_id?: string; oauth_scopes?: string[]; protocol_version?: string | null; server_info?: { name?: string; version?: string } };
type McpTool = { name: string; description?: string; inputSchema: Record<string, unknown> };
type Extension = { id: string; name: string; version: string; runtime: string; entry: string | null; contributes: string[]; permissions: string[]; enabled: boolean; status: string; signature_status?: string; sbom_path?: string | null; package_sha256?: string | null; installed_at?: string | null };
type Workspace = { workspace_id: string; name: string; path: string; session_count: number };
type PermissionRequest = { request_id: string; session_id?: string | null; subject_type: string; subject_id: string; action: string; details: Record<string, unknown>; status: string; created_at: string };
type PermissionGrant = { grant_id: string; subject_type: string; subject_id: string; action: string; scope: string; session_id?: string | null };
type RegistryServer = { name?: string; description?: string; server?: { name?: string; description?: string; version?: string } };
type MemoryItem = {
  memory_id: string; scope_type: "workspace" | "user"; scope_id: string;
  memory_type: "episodic" | "semantic" | "procedural"; memory_key: string;
  content: string; confidence: number; status: "active" | "candidate" | "superseded" | "deleted";
  pinned: boolean; created_reason: string; source_session_id: string; source_event_ids: string[];
  created_at: string; updated_at: string; last_verified_at: string; expires_at?: string | null;
  supersedes_memory_id?: string | null; conflict_memory_id?: string | null;
};
type MemorySearchResult = {
  memory: MemoryItem; channels: string[]; exact_match: boolean; fusion_score: number;
  lexical_score?: number | null; sparse_score?: number | null; sparse_query_coverage?: number | null;
  matched_terms: string[];
};
type MemoryAudit = {
  audit_id: string; memory_id: string; action: string; content_sha256: string;
  details: Record<string, unknown>; created_at: string;
};
type MemoryExport = {
  schema_version: number; exported_at: string; session_id: string; workspace_id?: string | null;
  memories: Array<MemoryItem & { audit: MemoryAudit[] }>;
};

const DEEPSEEK_PRESET = {
  model: "deepseek-v4-flash",
  base_url: "https://api.deepseek.com",
  context_window: 1_000_000
};

const api = <T,>(path: string, options?: { method?: string; body?: unknown }) => {
  if (!window.zagent) return Promise.reject(new Error("Core Bridge 不可用，请从 Z-Agent 桌面应用打开界面"));
  return window.zagent.request<T>(path, options);
};

function friendlyError(reason: unknown) {
  const message = reason instanceof Error ? reason.message : String(reason);
  return message.replace(/^Error invoking remote method 'core:request': Error:\s*/, "");
}

function payloadText(payload: unknown) {
  if (typeof payload === "string") return payload;
  const serialized = JSON.stringify(payload, null, 2);
  return serialized === undefined ? String(payload ?? "") : serialized;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function eventPreview(event: Event) {
  if (event.kind === "assistant_reasoning") return "思考过程（默认收起）";
  if (event.kind === "assistant_tool_calls" && typeof event.payload === "object" && event.payload) {
    const payload = event.payload as { tool_calls?: Array<{ name?: string }> };
    const names = (payload.tool_calls || []).map((call) => call.name).filter(Boolean);
    return names.length ? `工具调用：${names.join("、")}` : "工具调用";
  }
  if (event.role === "tool") return `${event.tool_name || "工具"} 已完成（详情默认隐藏）`;
  return payloadText(event.payload);
}

function modelLabel(model: ModelConfig) {
  return model.name || `${model.provider} · ${model.model}`;
}

function formatRelativeTime(iso: string) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return "刚刚";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.round(hours / 24);
  return `${days} 天前`;
}

function formatClockTime(iso: string) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function sessionTitleFromInput(content: string) {
  const compact = content.replace(/\s+/g, " ").trim();
  if (!compact) return "新任务";
  return compact.length <= 48 ? compact : `${compact.slice(0, 47).trimEnd()}…`;
}

function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string>();
  const [activeId, setActiveId] = useState<string>();
  const [events, setEvents] = useState<Event[]>([]);
  const [context, setContext] = useState<ContextStatus>();
  const [config, setConfig] = useState<AppConfig>();
  const [lastStats, setLastStats] = useState<TokenStats>();
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [runningSessionId, setRunningSessionId] = useState<string>();
  const [error, setError] = useState("");
  const [coreStatus, setCoreStatus] = useState<"online" | "recovering" | "offline">("online");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [extensionsOpen, setExtensionsOpen] = useState(false);
  const [memoriesOpen, setMemoriesOpen] = useState(false);
  const [extensions, setExtensions] = useState<Extension[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false);
  const [workspaceEditOpen, setWorkspaceEditOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [showToolTrace, setShowToolTrace] = useState(false);
  const [streaming, setStreaming] = useState<{ sessionId: string; text: string } | null>(null);
  const [taskActivity, setTaskActivity] = useState("");
  const [taskProgress, setTaskProgress] = useState<string[]>([]);
  const [pendingPermission, setPendingPermission] = useState<PermissionRequest>();
  const [permissionDecisionBusy, setPermissionDecisionBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [historyLoadingId, setHistoryLoadingId] = useState<string>();
  const activeRefreshGeneration = useRef(0);
  const sessionRefreshGeneration = useRef(0);
  const workspaceLandingId = useRef<string | undefined>(undefined);
  const activeIdRef = useRef<string | undefined>(undefined);

  const appendTaskProgress = useCallback((label: string) => {
    setTaskProgress((current) => current.at(-1) === label
      ? current
      : [...current, label].slice(-6));
  }, []);

  const refreshWorkspaces = useCallback(async () => {
    const response = await api<{ workspaces: Workspace[] }>("/v1/workspaces");
    setWorkspaces(response.workspaces);
    setActiveWorkspaceId((current) => response.workspaces.some((item) => item.workspace_id === current)
      ? current
      : response.workspaces[0]?.workspace_id);
    return response.workspaces;
  }, []);

  const refreshSessions = useCallback(async (workspaceId?: string) => {
    const generation = ++sessionRefreshGeneration.current;
    const query = workspaceId ? `?workspace_id=${workspaceId}` : "";
    const response = await api<{ sessions: Session[] }>(`/v1/sessions${query}`);
    if (generation !== sessionRefreshGeneration.current) return;
    setSessions(response.sessions);
    if (workspaceId && workspaceLandingId.current === workspaceId) {
      activeIdRef.current = undefined;
      setActiveId(undefined);
      setHistoryLoadingId(undefined);
      return;
    }
    const current = activeIdRef.current;
    const next = response.sessions.some((item) => item.session_id === current)
      ? current
      : response.sessions[0]?.session_id;
    if (next !== current) {
      activeIdRef.current = next;
      setHistoryLoadingId(next);
      setActiveId(next);
    }
  }, []);

  const refreshActive = useCallback(async (sessionId?: string) => {
    const generation = ++activeRefreshGeneration.current;
    if (!sessionId) {
      setHistoryLoadingId(undefined);
      return;
    }
    try {
      const [eventData, contextData] = await Promise.all([
        api<{ events: Event[] }>(`/v1/sessions/${sessionId}/events`),
        api<ContextStatus>(`/v1/sessions/${sessionId}/context`)
      ]);
      if (generation !== activeRefreshGeneration.current || activeIdRef.current !== sessionId) return;
      setEvents(eventData.events);
      setContext(contextData);
      setHistoryLoadingId((current) => current === sessionId ? undefined : current);
    } catch (reason) {
      if (generation === activeRefreshGeneration.current && activeIdRef.current === sessionId) {
        setHistoryLoadingId((current) => current === sessionId ? undefined : current);
      }
      throw reason;
    }
  }, []);

  useEffect(() => {
    Promise.all([refreshWorkspaces(), api<AppConfig>("/v1/config").then(setConfig)]).catch((reason) => setError(friendlyError(reason)));
  }, [refreshWorkspaces]);
  useEffect(() => {
    window.zagent?.onCoreStatus?.((status) => setCoreStatus(status.status));
  }, []);
  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);
  useEffect(() => {
    if (activeWorkspaceId) refreshSessions(activeWorkspaceId).catch((reason) => setError(friendlyError(reason)));
  }, [activeWorkspaceId, refreshSessions]);
  useEffect(() => { refreshActive(activeId).catch((reason) => setError(friendlyError(reason))); }, [activeId, refreshActive]);
  useEffect(() => { setShowToolTrace(false); }, [activeId]);
  useEffect(() => {
    if (!inspectorOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setInspectorOpen(false); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [inspectorOpen]);

  // --- auto-scroll: follow the newest content unless the user scrolled up ---
  const timelineRef = useRef<HTMLElement>(null);
  const stickToBottom = useRef(true);
  const prevActiveId = useRef<string | undefined>(undefined);
  const cancelRequested = useRef(false);
  const handleTimelineScroll = () => {
    const el = timelineRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };
  useEffect(() => {
    const el = timelineRef.current;
    if (!el) return;
    if (prevActiveId.current !== activeId) {
      prevActiveId.current = activeId;
      stickToBottom.current = true;
    }
    if (stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [events, busy, activeId]);

  async function createSession() {
    try {
      setMemoriesOpen(false);
      workspaceLandingId.current = undefined;
      const session = await api<Session>("/v1/sessions", { method: "POST", body: { title: "新任务", workspace_id: activeWorkspaceId } });
      await refreshSessions(activeWorkspaceId);
      activeIdRef.current = session.session_id;
      setActiveId(session.session_id);
      setHistoryLoadingId(undefined);
      setEvents([]);
      setContext(undefined);
    } catch (reason) {
      setError(friendlyError(reason));
    }
  }

  function selectSession(sessionId: string) {
    if (sessionId === activeIdRef.current) return;
    activeRefreshGeneration.current += 1;
    workspaceLandingId.current = undefined;
    activeIdRef.current = sessionId;
    setActiveId(sessionId);
    setHistoryLoadingId(sessionId);
    setEvents([]);
    setContext(undefined);
    setLastStats(undefined);
    setShowToolTrace(false);
    setMemoriesOpen(false);
    setError("");
  }

  async function createWorkspace(workspace: Workspace) {
    setWorkspaceDialogOpen(false);
    await refreshWorkspaces();
    activeRefreshGeneration.current += 1;
    sessionRefreshGeneration.current += 1;
    workspaceLandingId.current = workspace.workspace_id;
    setActiveWorkspaceId(workspace.workspace_id);
    setActiveId(undefined);
    setHistoryLoadingId(undefined);
    setSessions([]);
    setEvents([]);
    setContext(undefined);
    setLastStats(undefined);
    setStreaming(null);
    setTaskActivity("");
    setMemoriesOpen(false);
    setError("");
  }

  async function switchWorkspace(workspaceId: string) {
    activeRefreshGeneration.current += 1;
    sessionRefreshGeneration.current += 1;
    workspaceLandingId.current = undefined;
    setActiveWorkspaceId(workspaceId);
    setActiveId(undefined);
    setHistoryLoadingId(undefined);
    setSessions([]);
    setEvents([]);
    setContext(undefined);
    setLastStats(undefined);
    setMemoriesOpen(false);
    setStreaming(null);
    setTaskActivity("");
    setError("");
  }

  async function submitMessage(content: string) {
    if (!content.trim() || busy) return;
    let sessionId = activeId;
    let expectedContextVersion = sessionId ? context?.context_version : 0;
    setInput("");
    setBusy(true);
    setTaskActivity("正在分析任务");
    setTaskProgress(["正在建立任务请求"]);
    if (sessionId) setRunningSessionId(sessionId);
    cancelRequested.current = false;
    setError("");
    let optimistic: Event | null = null;
    let checkpointFailure: Checkpoint | undefined;
    try {
      if (!sessionId) {
        workspaceLandingId.current = undefined;
        const session = await api<Session>("/v1/sessions", { method: "POST", body: { title: sessionTitleFromInput(content), workspace_id: activeWorkspaceId } });
        sessionId = session.session_id;
        expectedContextVersion = 0;
        activeIdRef.current = sessionId;
        setActiveId(sessionId);
        setHistoryLoadingId(undefined);
        setRunningSessionId(sessionId);
      }
      const targetSessionId = sessionId;
      const currentSession = sessions.find((item) => item.session_id === targetSessionId);
      if (currentSession && ["新任务", "New task", "New Task"].includes(currentSession.title)) {
        const title = sessionTitleFromInput(content);
        setSessions((current) => current.map((item) => item.session_id === targetSessionId
          ? { ...item, title }
          : item));
      }
      // Optimistic render: show the user's message immediately after a session exists.
      const nextSequence = events.reduce((max, item) => Math.max(max, item.sequence), 0) + 1;
      optimistic = {
        event_id: `pending-${Date.now()}`, sequence: nextSequence,
        timestamp: new Date().toISOString(), kind: "message", role: "user",
        payload: content, token_estimate: 0,
      };
      setEvents((current) => [...current, optimistic!]);
      if (window.zagent.requestStream) {
        // Streaming path: reply text appears progressively while the model thinks.
        let reply = "";
        const outcome = await window.zagent.requestStream<{ stats: TokenStats }>(
          `/v1/sessions/${targetSessionId}/messages/stream`,
          { method: "POST", body: {
            content,
            ...(expectedContextVersion === undefined ? {} : { expected_context_version: expectedContextVersion })
          } },
          (streamEvent) => {
            if (streamEvent.type === "content") {
              reply += streamEvent.text || "";
              setStreaming({ sessionId: targetSessionId, text: reply });
              setTaskActivity("正在生成回答");
            } else if (streamEvent.type === "tool_call") {
              const label = `正在准备工具：${streamEvent.name || "未知工具"}`;
              setTaskActivity(label);
              appendTaskProgress(label);
            } else if (streamEvent.type === "tool_result") {
              const outcomeLabel = streamEvent.ok === false ? "执行失败，正在处理" : "已完成，正在继续";
              const label = `${streamEvent.name || "工具"}${outcomeLabel}${streamEvent.detail ? ` · ${streamEvent.detail}` : ""}`;
              setTaskActivity(label);
              appendTaskProgress(label);
            } else if (streamEvent.type === "permission_required" && streamEvent.request) {
              setPendingPermission(streamEvent.request);
              const label = `等待批准：${permissionSubjectLabel(streamEvent.request)}`;
              setTaskActivity(label);
              appendTaskProgress(label);
            } else if (streamEvent.type === "status") {
              const label = streamEvent.message || (streamEvent.round && streamEvent.round > 1
                ? `正在规划第 ${streamEvent.round} 步`
                : "正在分析任务");
              setTaskActivity(label);
              appendTaskProgress(label);
            }
          }
        );
        if (outcome?.type === "cancelled") {
          await Promise.all([refreshActive(targetSessionId), refreshSessions(activeWorkspaceId)]);
          return;
        }
        if (outcome?.checkpoint) checkpointFailure = outcome.checkpoint;
        if (outcome?.message) throw new Error(outcome.message);
        if (outcome?.result && (outcome.result as { stats?: TokenStats }).stats) {
          if (activeIdRef.current === targetSessionId) {
            setLastStats((outcome.result as { stats: TokenStats }).stats);
          }
        }
      } else {
        const result = await api<{ stats: TokenStats }>(`/v1/sessions/${targetSessionId}/messages`, {
          method: "POST",
          body: {
            content,
            ...(expectedContextVersion === undefined ? {} : { expected_context_version: expectedContextVersion })
          }
        });
        setLastStats(result.stats);
      }
      await Promise.all([refreshActive(targetSessionId), refreshSessions(activeWorkspaceId)]);
    } catch (reason) {
      if (!cancelRequested.current) {
        if (checkpointFailure && sessionId) {
          await Promise.all([refreshActive(sessionId), refreshSessions(activeWorkspaceId)]);
        } else if (optimistic) {
          setEvents((current) => current.filter((item) => item.event_id !== optimistic!.event_id));
        }
        if (!checkpointFailure) setInput((current) => current || content);
        setError(friendlyError(reason));
      }
    } finally {
      setBusy(false);
      setRunningSessionId(undefined);
      setStreaming(null);
      setTaskActivity("");
      setTaskProgress([]);
      setPendingPermission(undefined);
      setPermissionDecisionBusy(false);
    }
  }

  async function decideActivePermission(
    decision: "approved" | "denied",
    scope: "once" | "session"
  ) {
    if (!pendingPermission || permissionDecisionBusy) return;
    setPermissionDecisionBusy(true);
    setError("");
    try {
      await api(`/v1/permissions/requests/${pendingPermission.request_id}/decision`, {
        method: "POST",
        body: { decision, scope },
      });
      const label = decision === "approved" ? "已批准，正在安全执行" : "已拒绝，正在返回 Agent";
      setTaskActivity(label);
      appendTaskProgress(label);
      setPendingPermission(undefined);
    } catch (reason) {
      setError(friendlyError(reason));
    } finally {
      setPermissionDecisionBusy(false);
    }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    await submitMessage(input);
  }

  async function resumeTask() {
    await submitMessage(
      "继续当前任务。请使用系统提示中最新的 checkpoint，先按 event ID 和文件 SHA 核对已完成结果，再从 pending 继续，不要重复已完成的写入。"
    );
  }

  async function stopGeneration() {
    cancelRequested.current = true;
    setStreaming(null);
    setTaskActivity("");
    setTaskProgress([]);
    setShowToolTrace(false);
    if (window.zagent.cancelStream) await window.zagent.cancelStream();
  }

  async function togglePin(item: Event) {
    if (!activeId) return;
    const pinned = !!(context && context.working_set.pinned_event_ids.includes(item.event_id));
    try {
      await api(`/v1/sessions/${activeId}/context/tools`, {
        method: "POST",
        body: pinned
          ? { name: "context_unpin", arguments: { event_ids: [item.event_id] } }
          : { name: "context_pin", arguments: { event_ids: [item.event_id], rationale: "用户手动固定" } }
      });
      await refreshActive(activeId);
      setNotice(pinned
        ? "已取消固定：该事件之后可随会话增长退出工作集。"
        : "已固定为证据：它会跨最近窗口和归档持续发送给模型，除非超过模型硬上限。");
    } catch (reason) {
      setError(friendlyError(reason));
    }
  }

  async function switchModel(modelId: string) {
    try {
      setConfig(await api<AppConfig>(`/v1/models/${modelId}/activate`, { method: "POST" }));
    } catch (reason) {
      setError(friendlyError(reason));
    }
  }

  async function openExtensions() {
    try {
      const [ext, mcp] = await Promise.all([
        api<{ extensions: Extension[] }>("/v1/extensions"),
        api<{ servers: McpServer[] }>("/v1/mcp/servers")
      ]);
      setExtensions(ext.extensions); setMcpServers(mcp.servers); setExtensionsOpen(true);
    } catch (reason) {
      setError(friendlyError(reason));
    }
  }

  const budget = context?.working_set.budget || 0;
  const softLimit = config?.model && config.model.hard_limit_ratio > 0
    ? Math.round(budget / config.model.hard_limit_ratio * config.model.soft_limit_ratio)
    : 0;
  const ratio = budget > 0 ? (context?.working_set.tokens || 0) / budget * 100 : 0;
  const ratioText = ratio > 0 && ratio < 1 ? ratio.toFixed(2) : String(Math.round(ratio));
  const pinnedIds = new Set(context?.working_set.pinned_event_ids || []);
  const droppedPinnedIds = new Set(context?.working_set.dropped_pinned_ids || []);
  const activeTaskRunning = busy && runningSessionId === activeId;
  const blockedByOtherTask = busy && !activeTaskRunning;
  const historyLoading = !!activeId && historyLoadingId === activeId;
  const softRatioPct = budget > 0 && softLimit > 0 ? softLimit / budget * 100 : 0;
  const activeWorkspace = workspaces.find((item) => item.workspace_id === activeWorkspaceId);
  const isToolTrace = (item: Event) => item.kind === "assistant_tool_calls" || item.role === "tool";
  const hasReasoning = (item: Event) => item.kind === "assistant_reasoning"
    || (item.kind === "assistant_tool_calls" && typeof item.payload === "object" && item.payload !== null
      && typeof (item.payload as { reasoning_content?: unknown }).reasoning_content === "string");
  const toolTraceCount = events.filter(isToolTrace).length;
  const visibleEvents = events.filter((item) =>
    item.kind !== "model_raw"
    && item.kind !== "archive"
    && item.kind !== "checkpoint"
    && (showToolTrace || !isToolTrace(item) || hasReasoning(item))
  );

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">Z</span><div><strong>Z-Agent</strong><small>中文长程智能体</small></div></div>
      <div className="workspace-bar">
        <select className="workspace-switcher" disabled={busy} value={activeWorkspaceId || ""} onChange={(event) => switchWorkspace(event.target.value)} title="工作区 = agent 的安全边界（可访问目录）" aria-label="切换工作区">
          {workspaces.map((item) => <option key={item.workspace_id} value={item.workspace_id}>{item.name}{item.path ? ` · ${item.path}` : " · 未设置路径"}</option>)}
        </select>
        <button className="workspace-add" disabled={busy} onClick={() => setWorkspaceDialogOpen(true)} title="新建工作区" aria-label="新建工作区">＋</button>
        <button className="workspace-edit" disabled={!activeWorkspace || busy} onClick={() => setWorkspaceEditOpen(true)} title="编辑当前工作区的名称与路径" aria-label="编辑当前工作区">✎</button>
      </div>
      <button className="new-task" disabled={busy} onClick={createSession}>＋ 新建对话</button>
      <div className="section-label">最近任务</div>
      <nav className="session-list" aria-label="最近任务">{sessions.map((session) =>
        <button key={session.session_id} className={activeId === session.session_id ? "session active" : "session"} onClick={() => selectSession(session.session_id)} title={session.title}>
          <span><span className="session-title">{session.title}</span>{busy && runningSessionId === session.session_id && <i className="session-spinner" aria-label="正在执行" />}</span><small>{busy && runningSessionId === session.session_id ? taskActivity || "正在执行任务" : `${session.event_count} 个事件 · ${formatRelativeTime(session.updated_at)}`}</small>
        </button>)}</nav>
      <div className="sidebar-actions">
        <button disabled={!activeId} onClick={() => setMemoriesOpen(true)}>长期记忆{context?.memory_stats?.candidates ? ` · ${context.memory_stats.candidates} 待确认` : ""}</button>
        <button onClick={openExtensions}>扩展与 MCP</button>
        <button onClick={() => setSettingsOpen(true)}>模型设置</button>
      </div>
    </aside>

    <main className="workspace">
      <header className="topbar">
        <div className="topbar-main">
          <div className="topbar-title">
            <strong>{sessions.find((item) => item.session_id === activeId)?.title || "开始一个新任务"}</strong>
            <small>{activeWorkspace?.path || "工作区尚未设置项目路径"}</small>
          </div>
          {config && (config.models && config.models.length > 1
            ? <select className="model-switcher" value={config.active_model_id} onChange={(event) => switchModel(event.target.value)} title="切换模型" aria-label="切换模型">
                {config.models.map((model) => <option key={model.id} value={model.id}>{modelLabel(model)}</option>)}
              </select>
            : <span className="model-pill">{config.model.provider} · {config.model.model}</span>)}
        </div>
        <div className="topbar-actions">
          <span className={`status-dot status-${coreStatus}`}>
            {coreStatus === "online" ? "核心在线" : coreStatus === "recovering" ? "核心恢复中" : "核心离线"}
          </span>
          {toolTraceCount > 0 && <button type="button" className="tool-trace-toggle"
            onClick={() => setShowToolTrace((visible) => !visible)} aria-pressed={showToolTrace}
            aria-label={showToolTrace ? "隐藏工具记录" : "显示工具记录"}>
            工具记录 {toolTraceCount}
          </button>}
          <button type="button" className="inspector-toggle" onClick={() => setInspectorOpen((open) => !open)} aria-label="切换上下文检查器" aria-expanded={inspectorOpen}>上下文</button>
        </div>
      </header>
      <section className="timeline" ref={timelineRef} onScroll={handleTimelineScroll}>
        {historyLoading ? <div className="history-loading" role="status" aria-live="polite">
          <span className="history-loading-mark" aria-hidden="true" />
          <strong>正在加载对话历史</strong>
          <p>正在读取消息、工具记录与上下文状态…</p>
        </div> : <>
        {!activeWorkspace?.path && <div className="workspace-notice">
          <div><strong>先连接项目目录</strong><span>设置后才能读取和修改最新代码；密钥、凭据和工作区外文件始终不可访问。</span></div>
          <button type="button" onClick={() => setWorkspaceEditOpen(true)}>设置工作区</button>
        </div>}
        {!visibleEvents.length && <div className="empty-state"><div className="orb">Z</div><h1>从一个清晰的目标开始</h1><p>我会在工作区安全边界内阅读最新代码、调用工具并保留可追踪的上下文。</p><div className="empty-chips"><span>中文优先</span><span>代码可修改</span><span>敏感文件隔离</span></div></div>}
        {visibleEvents.map((item) => <EventCard key={item.event_id} item={item} pinned={pinnedIds.has(item.event_id)} onTogglePin={togglePin} showPin={!!context} showToolTrace={showToolTrace} />)}
        {activeTaskRunning && <div className="thinking task-running" role="status"><div className="thinking-current"><i></i><i></i><i></i><span>{taskActivity || "正在分析任务"}</span></div>{taskProgress.length > 1 && <ol className="task-progress">{taskProgress.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol>}</div>}
        {activeTaskRunning && streaming && streaming.sessionId === activeId && <article className="event assistant streaming-bubble">
          <div className="event-meta"><span>Z-Agent</span><code>正在输出…</code></div>
          <Markdown text={streaming.text || "…"} />
          <span className="streaming-caret" />
        </article>}
        </>}
      </section>
      {context?.latest_checkpoint && <div className="recovery-banner" role="status">
        <div><strong>任务已安全暂停</strong><span>{context.latest_checkpoint.reason === "max_tool_rounds" ? "已达本轮工具上限" : "已达本轮时间上限"}，进度已写入 {context.latest_checkpoint.checkpoint_id.slice(-12)}。</span></div>
        <button type="button" onClick={resumeTask} disabled={busy}>继续任务</button>
      </div>}
      {error && <div className="error-banner" role="alert"><span>{error}</span><button type="button" onClick={() => setError("")} aria-label="关闭错误提示">×</button></div>}
      {notice && <div className="notice-banner" role="status"><span>{notice}</span><button type="button" onClick={() => setNotice("")} aria-label="关闭提示">×</button></div>}
      <div className="composer-dock"><form className="composer" onSubmit={send}>
        <textarea disabled={blockedByOtherTask || historyLoading} value={input} onChange={(event) => setInput(event.target.value)} placeholder={historyLoading ? "正在加载对话历史…" : blockedByOtherTask ? "另一对话正在执行任务；可点击左侧加载项返回查看" : activeWorkspace?.path ? "描述任务，或让 Z-Agent 阅读并修改当前项目…" : "描述你的任务；如需处理代码，请先设置工作区"} aria-label="任务输入" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
        <button type={activeTaskRunning ? "button" : "submit"} className={`send-button${activeTaskRunning ? " stop" : ""}`} disabled={historyLoading || blockedByOtherTask || (!activeTaskRunning && !input.trim())} aria-label={activeTaskRunning ? "停止生成" : historyLoading ? "正在加载对话历史" : blockedByOtherTask ? "另一对话正在执行" : "发送消息"} title={activeTaskRunning ? "停止生成" : historyLoading ? "正在加载对话历史" : blockedByOtherTask ? "另一对话正在执行" : "发送"} onClick={activeTaskRunning ? stopGeneration : undefined}>{activeTaskRunning ? "■" : "↑"}</button>
      </form><small>{historyLoading ? "历史记录加载完成后即可继续输入" : blockedByOtherTask ? "当前运行中的对话已在左侧标记；同一窗口暂不并发执行任务" : "Enter 发送 · Shift+Enter 换行 · 文件修改会校验最新版本"}</small></div>
    </main>

    {inspectorOpen && <button className="inspector-scrim" type="button" onClick={() => setInspectorOpen(false)} aria-label="关闭上下文检查器" />}
    <aside className={`inspector${inspectorOpen ? " open" : ""}`} aria-label="上下文检查器" aria-hidden={!inspectorOpen} inert={!inspectorOpen}>
      <div className="inspector-title"><strong>上下文检查器</strong><div><span>LIVE</span><button type="button" onClick={() => setInspectorOpen(false)} aria-label="关闭上下文检查器">×</button></div></div>
      {context?.warning && <div className="context-warning" role="alert">{context.warning}</div>}
      <div className="meter" title={`工作集 = 本次发送给模型的内容（系统提示 + 最近事件 + 固定证据），受预算限制。预算 = 上下文窗口 × 硬上限比例（当前 ${budget.toLocaleString()} tokens）。达到软上限（${softLimit.toLocaleString()}）后新事件可能被挤出。`}>
        <div><span>工作集占用</span><strong>{context?.working_set.tokens ? context.working_set.tokens.toLocaleString() : 0} / {budget.toLocaleString()}</strong></div>
        <div className="meter-track">
          {softRatioPct > 0 && softRatioPct < 100 && <i className="meter-soft" style={{ left: `${softRatioPct}%` }} title={`软上限 ${softLimit.toLocaleString()} tokens`} />}
          <i className="meter-fill" style={{ width: ratio > 0 ? `max(${ratio}%, 4px)` : "0%" }} />
        </div>
        <small>已使用 {ratioText}% · 软上限 {Math.round((config?.model.soft_limit_ratio || 0) * 100)}%（{softLimit.toLocaleString()} tokens）{context?.working_set.pinned_tokens ? ` · 固定 ${context.working_set.pinned_tokens.toLocaleString()}` : ""}</small>
        <p className="context-explain">预算是安全上限，不是填充目标；系统只发送相关的最近事件（当前上限 {config?.recent_event_limit || 96}）和固定证据，避免用无关历史增加延迟与费用。</p>
      </div>
      <div className="stat-grid">
        <div title="事件 = 会话中的每一条记录：你的消息、模型回复、工具调用与结果、归档摘要等"><strong>{context?.stats.count || 0}</strong><span>事件</span></div>
        <div title="固定证据 = 被你手动固定的关键事件，会跨归档优先保留；超过模型硬上限时会显示警告"><strong>{pinnedIds.size}</strong><span>固定证据</span></div>
        <div title={`缓存命中率 = 命中缓存 token / (命中 + 未命中)。命中越多，首 token 延迟与费用越低（最近一次任务）`}><strong>{lastStats ? `${lastStats.cache_hit_rate}%` : "—"}</strong><span>缓存命中</span></div>
        <div title={`生成速度 = 完成任务生成的总 token 数 ÷ 总耗时（最近一次任务）`}><strong>{lastStats ? lastStats.tokens_per_second.toFixed(1) : "—"}</strong><span>tok/s</span></div>
      </div>
      {lastStats && <div className="inspector-section"><h3>最近任务</h3><p>{lastStats.total_tokens.toLocaleString()} tokens · 生成 {lastStats.completion_tokens.toLocaleString()} · 缓存 {lastStats.cache_hit_tokens.toLocaleString()}/{lastStats.cache_miss_tokens.toLocaleString()} · 耗时 {lastStats.elapsed_seconds.toFixed(1)}s</p></div>}
      <div className="inspector-section"><h3>固定证据（持续保留）</h3>
        <p className="context-explain">固定后会跨最近窗口和归档继续进入模型上下文；只有取消固定、工具轮不完整或超过模型真实硬上限时才会退出。</p>
        {context?.working_set.pinned_event_ids.length ? context.working_set.pinned_event_ids.map((id) => {
          const event = events.find((item) => item.event_id === id);
          const preview = event ? eventPreview(event).replace(/\s+/g, " ").slice(0, 42) : "";
          const dropped = droppedPinnedIds.has(id);
          return <div key={id} className={`ws-item${dropped ? " dropped" : ""}`}>
            <span className="ws-dot pinned" title={dropped ? "已固定但因硬上限未进入本轮" : "已固定并进入本轮上下文"}>●</span>
            <code>{id.slice(-12)}</code>
            <span className="ws-preview">{dropped ? "超过硬上限，本轮未包含" : preview || "较早事件（仍持续发送）"}</span>
            {event && <button className="ws-unpin" onClick={() => togglePin(event)} title="取消固定（点击后该事件可被预算清理）">取消</button>}
          </div>;
        }) : <p>暂无固定证据。可在消息底部点击“固定”。</p>}
      </div>
      <div className="inspector-section"><h3>最近工作集</h3>
        <p className="context-explain">模型本轮实际收到 {context?.working_set.included_event_ids.length || 0} 个事件；下方只预览最近 10 条普通事件，固定证据已在上方单独列出。</p>
        {context?.working_set.included_event_ids.some((id) => !pinnedIds.has(id)) ? context.working_set.included_event_ids.filter((id) => !pinnedIds.has(id)).slice(-10).reverse().map((id) => {
          const event = events.find((item) => item.event_id === id);
          const preview = event ? eventPreview(event).replace(/\s+/g, " ").slice(0, 42) : "";
          return <div key={id} className="ws-item"><span className="ws-dot" title="普通最近事件">·</span><code>{id.slice(-12)}</code>{preview && <span className="ws-preview">{preview}</span>}</div>;
        }) : <p>暂无普通事件</p>}
      </div>
      <div className="inspector-section"><h3>最近归档</h3>{context?.latest_archive
        ? <div className="archive-card">
            <code>{context.latest_archive.archive_id}</code>
            <pre>{JSON.stringify(context.latest_archive.state, null, 2)}</pre>
            <p>已外置 {context.archive_stats?.count ?? "—"} 个事件（约 {context.archive_stats?.tokens.toLocaleString() ?? "—"} tokens）。结构化状态已注入系统提示词；原文仍可检索和按 event ID 取回。</p>
          </div>
        : <p>模型在完成任务阶段后会调用归档工具，在这里留下任务状态摘要。你也可以通过对话要求"归档当前阶段"。</p>}</div>
      {context?.latest_checkpoint && <div className="inspector-section"><h3>可恢复 CHECKPOINT</h3>
        <div className="archive-card"><code>{context.latest_checkpoint.checkpoint_id}</code>
          <p>已完成 {context.latest_checkpoint.state.completed?.length || 0} 个工具调用，待处理 {context.latest_checkpoint.state.pending?.length || 0} 项。续跑时会核对证据 event ID 和文件 SHA。</p>
        </div></div>}
    </aside>

    {settingsOpen && config && <Settings config={config} onClose={() => setSettingsOpen(false)} onSaved={(next) => { setConfig(next); }} />}
    {workspaceDialogOpen && <CreateWorkspaceModal onClose={() => setWorkspaceDialogOpen(false)} onCreated={createWorkspace} />}
    {workspaceEditOpen && workspaces.find((item) => item.workspace_id === activeWorkspaceId) && (
      <EditWorkspaceModal workspace={workspaces.find((item) => item.workspace_id === activeWorkspaceId)!}
        onClose={() => setWorkspaceEditOpen(false)}
        onSaved={async () => { setWorkspaceEditOpen(false); await refreshWorkspaces(); }} />
    )}
    {extensionsOpen && <ExtensionsModal extensions={extensions} mcpServers={mcpServers}
      onClose={() => setExtensionsOpen(false)}
      onChange={(ext, mcp) => { setExtensions(ext); setMcpServers(mcp); }}
      onError={setError} />}
    {memoriesOpen && activeId && <MemoryModal sessionId={activeId}
      onClose={() => setMemoriesOpen(false)} onError={setError} />}
    {pendingPermission && <PermissionDialog request={pendingPermission}
      busy={permissionDecisionBusy}
      onDecision={decideActivePermission} />}
  </div>;
}

function permissionSubjectLabel(request: PermissionRequest) {
  if (request.subject_type === "runner") return `运行测试 ${request.subject_id}`;
  if (request.subject_type === "mcp") return `调用 MCP ${request.subject_id}`;
  if (request.subject_type === "extension") return `调用扩展 ${request.subject_id}`;
  return `${request.subject_type} ${request.subject_id}`;
}

function PermissionDialog({ request, busy, onDecision }: {
  request: PermissionRequest;
  busy: boolean;
  onDecision(decision: "approved" | "denied", scope: "once" | "session"): void;
}) {
  const command = Array.isArray(request.details.command_template)
    ? request.details.command_template.map(String).join(" ")
    : "";
  const network = request.details.network === true;
  return <div className="permission-backdrop">
    <section className="permission-dialog" role="alertdialog" aria-modal="true" aria-label="需要批准">
      <header><span className="permission-icon">!</span><div><h2>需要批准</h2><p>Z-Agent 想要执行一个受保护操作</p></div></header>
      <div className="permission-body">
        <strong>{permissionSubjectLabel(request)}</strong>
        {command && <div className="permission-command"><span>将运行</span><code>{command}</code></div>}
        <dl>
          <div><dt>范围</dt><dd>{request.session_id ? "当前对话" : "应用级"}</dd></div>
          <div><dt>网络</dt><dd>{network ? "允许" : "禁止"}</dd></div>
          {request.subject_type === "runner" && <div><dt>文件</dt><dd>去敏只读快照</dd></div>}
        </dl>
        <p>批准后将继续当前工具调用；拒绝会把结果返回给 Agent。测试命令不能由模型任意修改。</p>
      </div>
      <footer>
        <button type="button" className="btn-ghost" disabled={busy} onClick={() => onDecision("denied", "once")}>拒绝</button>
        <button type="button" className="btn-ghost" disabled={busy || !request.session_id} onClick={() => onDecision("approved", "session")}>本次对话允许</button>
        <button type="button" className="permission-approve" disabled={busy} onClick={() => onDecision("approved", "once")}>{busy ? "处理中…" : "仅此一次"}</button>
      </footer>
    </section>
  </div>;
}

function Modal({ title, onClose, children }: { title: string; onClose(): void; children: ReactNode }) {
  const modalRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const dialogs = document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]');
      if (dialogs.item(dialogs.length - 1) === modalRef.current) onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
    <section ref={modalRef} className="modal" role="dialog" aria-modal="true" aria-label={title}>
      <header><h2>{title}</h2><button type="button" onClick={onClose} aria-label={`关闭${title}`}>×</button></header>
      <div className="modal-content">{children}</div>
    </section>
  </div>;
}

function Collapsible({ summary, children, defaultOpen }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(!!defaultOpen);
  return <div className={`collapsible${open ? " open" : ""}`}>
    <button type="button" className="collapsible-summary" aria-expanded={open} onClick={() => setOpen(!open)}><span className="chevron">{open ? "▾" : "▸"}</span>{summary}</button>
    {open && <div className="collapsible-body">{children}</div>}
  </div>;
}

function EventCard({ item, pinned, onTogglePin, showPin, showToolTrace }: {
  item: Event; pinned: boolean; onTogglePin(item: Event): void; showPin: boolean; showToolTrace: boolean;
}) {
  const isToolCallEvent = item.kind === "assistant_tool_calls";
  const isToolResult = item.role === "tool";
  const isStandaloneReasoning = item.kind === "assistant_reasoning";
  const payload = item.payload;
  const toolCallPayload = isToolCallEvent && typeof payload === "object" && payload !== null ? payload as { content?: string; reasoning_content?: string; tool_calls?: Array<{ call_id: string; name: string; arguments: unknown }> } : null;
  const reasoning = isStandaloneReasoning && typeof payload === "string" ? payload : toolCallPayload?.reasoning_content;

  const reasoningDisclosure = reasoning ? <Collapsible summary={<span className="reasoning-label">思考过程 <small>默认收起</small></span>}>
    <Markdown text={reasoning} className="reasoning-content" />
  </Collapsible> : null;

  function renderBody() {
    if (isStandaloneReasoning) return reasoningDisclosure;
    if (toolCallPayload) {
      return <div className="tool-event-body">
        {reasoningDisclosure}
        {showToolTrace && (toolCallPayload.tool_calls || []).map((call) =>
          <Collapsible key={call.call_id} defaultOpen={false}
            summary={<span className="tool-line"><span className="tool-line-name">{call.name}</span>
              <span className="tool-line-args">{safeArgsSummary(call.arguments)}</span></span>}>
            <CodeBlock code={payloadText(call.arguments)} language={isRecord(call.arguments) || Array.isArray(call.arguments) ? "json" : "text"} label="调用参数" />
          </Collapsible>)}
      </div>;
    }
    if (isToolResult) {
      const summary = toolResultSummary(payload);
      return <Collapsible summary={<span className="tool-line"><span className="tool-line-name">{item.tool_name || "tool_result"}</span><span className="tool-line-args">{summary}</span></span>}>
        <ToolResultBody item={item} />
      </Collapsible>;
    }
    if (item.role === "assistant" && typeof item.payload === "string") return <Markdown text={item.payload} />;
    return <pre>{payloadText(item.payload)}</pre>;
  }

  const canPin = showPin && !isToolCallEvent && !isToolResult && !isStandaloneReasoning && item.role !== "system";
  return <article className={`event ${item.role}${isToolCallEvent || isToolResult ? " tool-event" : ""}${reasoning ? " reasoning-event" : ""}${pinned ? " pinned" : ""}`}>
    <div className="event-meta"><span>{item.role === "user" ? "你" : item.role === "assistant" ? "Z-Agent" : item.kind}</span><code>#{item.sequence} · {formatClockTime(item.timestamp)} · {item.event_id.slice(-8)}</code></div>
    {renderBody()}
    {(item.tool_name || canPin) && <div className="event-foot">
      {item.tool_name && <span className="tool-tag">{item.tool_name}</span>}
      {canPin && <button className={pinned ? "pin-btn active" : "pin-btn"} onClick={() => onTogglePin(item)} title={pinned ? "取消固定：该事件之后可被工作集预算清理" : "固定为证据：即使会话变长、事件超出工作集预算，这条也始终会发给模型，不会被挤掉"}>
        {pinned ? "已固定" : "固定"}
      </button>}
    </div>}
  </article>;
}

function toolResultSummary(payload: unknown) {
  if (isRecord(payload)) {
    if (typeof payload.path === "string") return `${payload.path}${payload.truncated === true ? " · 已截断" : ""}`;
    if (typeof payload.error === "string") return payload.error.replace(/\s+/g, " ").slice(0, 80);
  }
  return payloadText(payload).replace(/\s+/g, " ").slice(0, 80);
}

function ToolResultBody({ item }: { item: Event }) {
  const payload = item.payload;
  if (isRecord(payload) && typeof payload.content === "string") {
    const path = typeof payload.path === "string" ? payload.path : undefined;
    const metadata = Object.fromEntries(Object.entries(payload).filter(([key]) => key !== "content"));
    return <div className="tool-result-content">
      <CodeBlock code={payload.content} language={languageFromPath(path)} label={path || `${item.tool_name || "工具"} 输出`} />
      {!!Object.keys(metadata).length && <Collapsible summary="结果元数据">
        <CodeBlock code={payloadText(metadata)} language="json" label="metadata" />
      </Collapsible>}
    </div>;
  }
  const structured = typeof payload === "object" && payload !== null;
  return <CodeBlock code={payloadText(payload)} language={structured ? "json" : "text"} label={structured ? "工具结果" : `${item.tool_name || "工具"} 输出`} />;
}

function safeArgsSummary(argumentsValue: unknown) {
  try {
    const text = typeof argumentsValue === "string" ? argumentsValue : JSON.stringify(argumentsValue);
    return text.replace(/\s+/g, " ").slice(0, 60);
  } catch (_) {
    return "";
  }
}

function ConfirmDialog({ message, confirmText, onCancel, onConfirm }: {
  message: string; confirmText?: string; onCancel(): void; onConfirm(): void;
}) {
  return <Modal title="确认操作" onClose={onCancel}>
    <p className="confirm-message">{message}</p>
    <div className="form-actions"><button onClick={onCancel}>取消</button><button className="btn-danger-solid" onClick={onConfirm}>{confirmText || "确认"}</button></div>
  </Modal>;
}

function memoryTypeLabel(type: MemoryItem["memory_type"]) {
  return type === "semantic" ? "事实" : type === "procedural" ? "做法" : "经历";
}

function MemoryModal({ sessionId, onClose, onError }: {
  sessionId: string; onClose(): void; onError(message: string): void;
}) {
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [results, setResults] = useState<MemorySearchResult[] | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string>();
  const [confirmDelete, setConfirmDelete] = useState<MemoryItem | null>(null);
  const [correcting, setCorrecting] = useState<MemoryItem | null>(null);
  const [correctionContent, setCorrectionContent] = useState("");
  const [audits, setAudits] = useState<Record<string, MemoryAudit[]>>({});
  const [error, setError] = useState("");
  const [exportedPath, setExportedPath] = useState("");
  const requestGeneration = useRef(0);

  const fail = useCallback((reason: unknown) => {
    const message = friendlyError(reason);
    setError(message);
    onError(message);
  }, [onError]);

  const refresh = useCallback(async () => {
    const generation = ++requestGeneration.current;
    setLoading(true);
    try {
      const response = await api<{ memories: MemoryItem[] }>(
        `/v1/sessions/${sessionId}/memories?include_candidates=true&limit=100`
      );
      if (generation !== requestGeneration.current) return;
      setMemories(response.memories);
      setResults(null);
    } finally {
      if (generation === requestGeneration.current) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    refresh().catch(fail);
    return () => { requestGeneration.current += 1; };
  }, [refresh, fail]);

  async function search(event: FormEvent) {
    event.preventDefault();
    const clean = query.trim();
    if (!clean) { await refresh(); return; }
    const generation = ++requestGeneration.current;
    setLoading(true); setError("");
    try {
      const response = await api<{ results: MemorySearchResult[] }>(
        `/v1/sessions/${sessionId}/memories?query=${encodeURIComponent(clean)}&limit=20`
      );
      if (generation !== requestGeneration.current) return;
      setResults(response.results);
    } catch (reason) {
      if (generation === requestGeneration.current) fail(reason);
    }
    finally {
      if (generation === requestGeneration.current) setLoading(false);
    }
  }

  async function confirm(memory: MemoryItem) {
    setBusyId(memory.memory_id); setError("");
    try {
      await api(`/v1/sessions/${sessionId}/memories/${memory.memory_id}/confirm`, {
        method: "POST",
        body: memory.conflict_memory_id ? { supersedes_memory_id: memory.conflict_memory_id } : {},
      });
      await refresh();
    } catch (reason) {
      fail(reason);
      await refresh().catch(fail);
    }
    finally { setBusyId(undefined); }
  }

  async function removeConfirmed() {
    const memory = confirmDelete;
    if (!memory) return;
    setConfirmDelete(null); setBusyId(memory.memory_id); setError("");
    try {
      await api(`/v1/sessions/${sessionId}/memories/${memory.memory_id}`, {
        method: "DELETE", body: { reason: "用户在长期记忆管理界面中删除" },
      });
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusyId(undefined); }
  }

  async function togglePinned(memory: MemoryItem) {
    setBusyId(memory.memory_id); setError("");
    try {
      await api(`/v1/sessions/${sessionId}/memories/${memory.memory_id}`, {
        method: "PATCH",
        body: { pinned: !memory.pinned, expected_pinned: memory.pinned },
      });
      await refresh();
    } catch (reason) {
      fail(reason);
      await refresh().catch(fail);
    }
    finally { setBusyId(undefined); }
  }

  async function exportMemories() {
    if (!window.zagent.saveJson) {
      fail(new Error("记忆导出需要 Electron 桌面端"));
      return;
    }
    setBusyId("export"); setError(""); setExportedPath("");
    try {
      const payload = await api<MemoryExport>(`/v1/sessions/${sessionId}/memories/export`);
      const day = new Date().toISOString().slice(0, 10);
      const saved = await window.zagent.saveJson(
        `zagent-memories-${day}.json`,
        `${JSON.stringify(payload, null, 2)}\n`,
      );
      if (saved) setExportedPath(saved);
    } catch (reason) { fail(reason); }
    finally { setBusyId(undefined); }
  }

  async function saveCorrection(event: FormEvent) {
    event.preventDefault();
    const memory = correcting;
    const content = correctionContent.trim();
    if (!memory || !content || content === memory.content) return;
    setBusyId(memory.memory_id); setError("");
    try {
      await api(`/v1/sessions/${sessionId}/memories/${memory.memory_id}/correct`, {
        method: "POST",
        body: { content, reason: "用户在长期记忆管理界面中纠正" },
      });
      setCorrecting(null);
      setCorrectionContent("");
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusyId(undefined); }
  }

  async function loadAudit(memoryId: string) {
    if (audits[memoryId]) {
      setAudits((current) => { const next = { ...current }; delete next[memoryId]; return next; });
      return;
    }
    setBusyId(memoryId); setError("");
    try {
      const response = await api<{ audit: MemoryAudit[] }>(
        `/v1/sessions/${sessionId}/memories/${memoryId}/audit?limit=50`
      );
      setAudits((current) => ({ ...current, [memoryId]: response.audit }));
    } catch (reason) { fail(reason); }
    finally { setBusyId(undefined); }
  }

  const operationBusy = busyId !== undefined;
  const displayed = results ? results.map((item) => item.memory) : memories;
  const resultById = new Map((results || []).map((item) => [item.memory.memory_id, item]));
  return <Modal title="长期记忆" onClose={onClose}><div className="memory-manager">
    <p className="settings-hint">成功写入或修改工作区文件并完成回复后，会自动生成“待确认”任务记忆；普通聊天、只读分析、失败或取消不触发。候选不会进入模型上下文，由你确认后才会跨会话召回。更新前已完成的任务不会自动追溯生成。</p>
    <form className="memory-search" onSubmit={search}>
      <input value={query} disabled={operationBusy} onChange={(event) => setQuery(event.target.value)} placeholder="搜索事实、偏好或工作方法" aria-label="搜索长期记忆" autoFocus />
      <button disabled={loading || operationBusy}>搜索</button>
      {results && <button type="button" className="btn-ghost" disabled={operationBusy} onClick={() => { setQuery(""); refresh().catch(fail); }}>清除</button>}
    </form>
    {error && <div className="settings-error" role="alert">{error}</div>}
    <div className="memory-summary">
      <span>{results ? `找到 ${displayed.length} 条` : `${memories.filter((item) => item.status === "active").length} 条生效 · ${memories.filter((item) => item.status === "candidate").length} 条待确认`}</span>
      <small>用户级记忆跨工作区；工作区级记忆只在当前项目内使用</small>
      <button type="button" className="btn-ghost" disabled={operationBusy || loading} onClick={exportMemories}>导出 JSON</button>
    </div>
    {exportedPath && <div className="memory-exported" role="status">已导出到 <code>{exportedPath}</code></div>}
    <div className="memory-list">
      {loading && <div className="memory-empty">正在读取记忆…</div>}
      {!loading && !displayed.length && <div className="memory-empty">{results ? "没有相关记忆" : "尚未形成长期记忆"}</div>}
      {!loading && displayed.map((memory) => {
        const explanation = resultById.get(memory.memory_id);
        return <article className={`memory-card ${memory.status}`} key={memory.memory_id}>
          <div className="memory-head"><div className="memory-badges">
            <span>{memory.status === "active" ? "已生效" : "待确认"}</span>
            <span>{memoryTypeLabel(memory.memory_type)}</span>
            <span>{memory.scope_type === "user" ? "用户级" : "工作区级"}</span>
            {memory.pinned && <span>固定</span>}
          </div><code>{memory.memory_id.slice(-8)}</code></div>
          <h3>{memory.memory_key}</h3>
          <p className="memory-content">{memory.content}</p>
          {memory.conflict_memory_id && <p className="memory-conflict">确认后将替换同名旧记忆 {memory.conflict_memory_id.slice(-8)}</p>}
          {explanation && <p className="memory-match">召回：{explanation.exact_match ? "精确命中" : explanation.channels.join(" + ") || "排序命中"}
            {explanation.matched_terms.length ? ` · 匹配 ${explanation.matched_terms.slice(0, 8).join("、")}` : ""}
            {explanation.sparse_query_coverage != null ? ` · 覆盖 ${Math.round(explanation.sparse_query_coverage * 100)}%` : ""}</p>}
          <div className="memory-meta"><span>可信度 {Math.round(memory.confidence * 100)}%</span><span>证据 {memory.source_event_ids.length} 条</span><span>核验于 {formatRelativeTime(memory.last_verified_at)}</span></div>
          {audits[memory.memory_id] && <div className="memory-audit" aria-label={`${memory.memory_key} 审计记录`}>
            {audits[memory.memory_id].map((entry) => <div key={entry.audit_id}><span>{entry.action}</span><small>{formatRelativeTime(entry.created_at)}</small><code>{entry.content_sha256.slice(0, 12)}</code></div>)}
          </div>}
          <div className="memory-actions">
            <button className="btn-ghost" disabled={operationBusy} onClick={() => loadAudit(memory.memory_id)}>{audits[memory.memory_id] ? "收起审计" : "查看审计"}</button>
            {memory.status === "active" && <button className="btn-ghost" disabled={operationBusy} onClick={() => togglePinned(memory)}>{memory.pinned ? "取消固定" : "固定"}</button>}
            {memory.status === "active" && <button className="btn-ghost" disabled={operationBusy} onClick={() => { setCorrecting(memory); setCorrectionContent(memory.content); }}>纠正</button>}
            {memory.status === "candidate" && <button disabled={operationBusy} onClick={() => confirm(memory)}>{memory.conflict_memory_id ? "确认并替换" : "确认生效"}</button>}
            <button className="btn-danger" disabled={operationBusy} onClick={() => setConfirmDelete(memory)}>删除</button>
          </div>
        </article>;
      })}
    </div>
    {confirmDelete && <ConfirmDialog message={`删除长期记忆「${confirmDelete.memory_key}」？原文会被清空，并保留不可召回的审计墓碑。`} confirmText="确认删除"
      onCancel={() => setConfirmDelete(null)} onConfirm={removeConfirmed} />}
    {correcting && <Modal title="纠正长期记忆" onClose={() => { if (!operationBusy) setCorrecting(null); }}>
      <form className="settings" onSubmit={saveCorrection}>
        <p className="settings-hint">旧记忆不会被原地改写；系统会保留它的审计链，并用新版本显式替代。</p>
        <label>记忆正文<textarea aria-label="纠正后的记忆正文" value={correctionContent} onChange={(event) => setCorrectionContent(event.target.value)} rows={6} autoFocus /></label>
        <div className="form-actions"><button type="button" disabled={operationBusy} onClick={() => setCorrecting(null)}>取消</button><button disabled={operationBusy || !correctionContent.trim() || correctionContent.trim() === correcting.content}>保存并替换</button></div>
      </form>
    </Modal>}
  </div></Modal>;
}

function EditWorkspaceModal({ workspace, onClose, onSaved }: {
  workspace: Workspace; onClose(): void; onSaved(): void;
}) {
  const [name, setName] = useState(workspace.name);
  const [path, setPath] = useState(workspace.path || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      await api(`/v1/workspaces/${workspace.workspace_id}`, { method: "PATCH", body: { name: name.trim(), path: path.trim() } });
      onSaved();
    } catch (reason) {
      setError(friendlyError(reason));
    } finally { setSaving(false); }
  }
  return <Modal title="编辑工作区" onClose={onClose}><form className="settings" onSubmit={submit}>
    <p className="settings-hint">工作区路径是文件安全边界。Z-Agent 只能在这里读取和修改文本代码；修改前会校验最新版本，且不会读取 .env、私钥、凭据或工作区外文件。</p>
    <label>名称<input value={name} onChange={(event) => setName(event.target.value)} autoFocus /></label>
    <label className="path-field">路径
      <div className="path-row">
        <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="选择或输入项目目录路径" />
        {window.zagent.selectFolder && <button type="button" className="btn-ghost" onClick={async () => {
          const selected = await window.zagent.selectFolder!();
          if (selected) setPath(selected);
        }}>选择文件夹…</button>}
      </div>
    </label>
    {error && <div className="settings-error" role="alert">{error}</div>}
    <div className="form-actions"><button type="button" onClick={onClose}>取消</button><button disabled={saving}>保存</button></div>
  </form></Modal>;
}

function CreateWorkspaceModal({ onClose, onCreated }: { onClose(): void; onCreated(workspace: Workspace): void }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const response = await api<{ workspace: Workspace }>("/v1/workspaces", { method: "POST", body: { name: name.trim(), path: path.trim() } });
      onCreated(response.workspace);
    } catch (reason) {
      setError(friendlyError(reason));
    } finally { setSaving(false); }
  }
  return <Modal title="新建工作区" onClose={onClose}><form className="settings" onSubmit={submit}>
    <p className="settings-hint">工作区限定代码工具的访问范围。可以读取和修改其中的非敏感文本文件；.env、私钥、凭据、二进制和目录外路径始终拒绝。每个工作区拥有独立会话。</p>
    <label>名称<input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：项目 A" autoFocus /></label>
    <label className="path-field">路径（agent 可访问的项目目录）
      <div className="path-row">
        <input value={path} onChange={(event) => setPath(event.target.value)} placeholder="/Users/me/projects/project-a" />
        {window.zagent.selectFolder && <button type="button" className="btn-ghost" onClick={async () => {
          const selected = await window.zagent.selectFolder!();
          if (selected) setPath(selected);
        }}>选择文件夹…</button>}
      </div>
    </label>
    {error && <div className="settings-error" role="alert">{error}</div>}
    <div className="form-actions"><button type="button" onClick={onClose}>取消</button><button disabled={saving || !name.trim()}>创建工作区</button></div>
  </form></Modal>;
}

const EMPTY_FORM = { name: "", provider: "openai_compatible", model: "", base_url: "", api_key: "", context_window: 32768 };

function Settings({ config, onClose, onSaved }: { config: AppConfig; onClose(): void; onSaved(value: AppConfig): void }) {
  const [models, setModels] = useState(config.models);
  const [activeId, setActiveId] = useState(config.active_model_id);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<{ name: string; id: string } | null>(null);

  function beginNew() {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setError("");
  }
  function beginEdit(model: ModelConfig) {
    setEditingId(model.id);
    setForm({ name: model.name || "", provider: model.provider, model: model.model, base_url: model.base_url, api_key: "", context_window: model.context_window });
    setError("");
  }
  function selectProvider(provider: string) {
    setError("");
    setForm(provider === "deepseek" ? { ...form, provider, ...DEEPSEEK_PRESET } : { ...form, provider });
  }
  function applyConfig(next: AppConfig) {
    setModels(next.models);
    setActiveId(next.active_model_id);
    onSaved(next);
  }
  async function activate(modelId: string) {
    try { applyConfig(await api<AppConfig>(`/v1/models/${modelId}/activate`, { method: "POST" })); }
    catch (reason) { setError(friendlyError(reason)); }
  }
  async function removeConfirmed() {
    if (!confirmDelete) return;
    const modelId = confirmDelete.id;
    setConfirmDelete(null);
    try { applyConfig(await api<AppConfig>(`/v1/models/${modelId}`, { method: "DELETE" })); }
    catch (reason) { setError(friendlyError(reason)); }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const body = { ...form, api_key: form.api_key || undefined };
      const next = editingId
        ? await api<AppConfig>(`/v1/models/${editingId}`, { method: "PATCH", body })
        : await api<AppConfig>("/v1/models", { method: "POST", body });
      applyConfig(next);
      setEditingId(null);
      setForm(EMPTY_FORM);
    } catch (reason) {
      setError(friendlyError(reason));
    } finally { setSaving(false); }
  }
  return <Modal title="模型设置" onClose={onClose}><div className="settings">
    <section className="settings-section">
      <div className="settings-head"><h3>模型列表</h3><button type="button" className="btn-ghost" onClick={beginNew}>＋ 新建模型</button></div>
      <div className="model-list">
        {models.map((model) => <div key={model.id} className={model.id === activeId ? "model-item active" : "model-item"}>
          <button type="button" className="model-activate" title={model.id === activeId ? "当前模型" : "切换到此模型"} onClick={() => activate(model.id)}>{model.id === activeId ? "✓" : ""}</button>
          <div className="model-info"><strong>{modelLabel(model)}</strong><small>{model.base_url || "本地演示"}</small></div>
          <button type="button" className="btn-ghost" onClick={() => beginEdit(model)}>编辑</button>
          <button type="button" className="btn-danger" onClick={() => setConfirmDelete({ name: modelLabel(model), id: model.id })}>删除</button>
        </div>)}
      </div>
    </section>
    <form className="settings-form" onSubmit={save}>
      <div className="settings-head"><h3>{editingId ? "编辑模型" : "新建模型"}</h3>{editingId && <button type="button" className="btn-ghost" onClick={beginNew}>改为新建</button>}</div>
      <label>名称<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：DeepSeek 主力" /></label>
      <label>适配器<select value={form.provider} onChange={(event) => selectProvider(event.target.value)}><option value="echo">本地演示</option><option value="openai_compatible">OpenAI Compatible</option><option value="qwen">通义千问</option><option value="deepseek">DeepSeek</option><option value="glm">智谱 GLM</option><option value="kimi">Kimi</option><option value="minimax">MiniMax</option></select></label>
      <label>模型名称<input value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} /></label>
      <label>API Base URL<input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://.../v1" /></label>
      {form.provider === "deepseek" && <p className="settings-hint">官方端点使用 <code>https://api.deepseek.com</code>；默认模型为 <code>deepseek-v4-flash</code>。</p>}
      <label>API Key<input type="password" value={form.api_key} onChange={(event) => setForm({ ...form, api_key: event.target.value })} placeholder="不填写则保持现有密钥" /></label>
      <label>上下文窗口<input type="number" value={form.context_window} onChange={(event) => setForm({ ...form, context_window: Number(event.target.value) })} /></label>
      {error && <div className="settings-error" role="alert">{error}</div>}
      <div className="form-actions"><button type="button" onClick={onClose}>完成</button><button disabled={saving}>{editingId ? "保存修改" : "添加模型"}</button></div>
    </form>
    {confirmDelete && <ConfirmDialog message={`删除模型配置「${confirmDelete.name}」？`} confirmText="确认删除"
      onCancel={() => setConfirmDelete(null)} onConfirm={removeConfirmed} />}
  </div></Modal>;
}

const MCP_TRANSPORTS = ["stdio", "http", "sse"];
const EXTENSION_RUNTIMES = ["declarative", "node", "python"];
const EXTENSION_CONTRIBUTIONS = ["tools", "views", "skills", "model_providers", "context_sources"];

function ExtensionsModal({ extensions, mcpServers, onClose, onChange, onError }: {
  extensions: Extension[]; mcpServers: McpServer[]; onClose(): void;
  onChange(extensions: Extension[], mcpServers: McpServer[]): void;
  onError(message: string): void;
}) {
  const [mcpForm, setMcpForm] = useState({ name: "", transport: "stdio", command: "", args: "", cwd: "", env: "", url: "", enabled: true, approved: false, sandbox: true, network: false, oauth: false, oauth_client_id: "", oauth_scopes: "" });
  const [mcpImportPath, setMcpImportPath] = useState("");
  const [extForm, setExtForm] = useState({ id: "", name: "", runtime: "declarative", entry: "", contributes: ["tools"] as string[] });
  const [importPath, setImportPath] = useState("");
  const [importEnabled, setImportEnabled] = useState(false);
  const [mcpTools, setMcpTools] = useState<Record<string, McpTool[]>>({});
  const [extensionTools, setExtensionTools] = useState<Record<string, McpTool[]>>({});
  const [permissionRequests, setPermissionRequests] = useState<PermissionRequest[]>([]);
  const [permissionGrants, setPermissionGrants] = useState<PermissionGrant[]>([]);
  const [hostPermission, setHostPermission] = useState<{ request: PermissionRequest; extension: Extension }>();
  const [registryQuery, setRegistryQuery] = useState("");
  const [registryServers, setRegistryServers] = useState<RegistryServer[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<{ message: string; kind: "mcp" | "extension"; id: string } | null>(null);

  useEffect(() => { refreshPermissions().catch(fail); }, []);

  async function refresh() {
    const [ext, mcp] = await Promise.all([
      api<{ extensions: Extension[] }>("/v1/extensions"),
      api<{ servers: McpServer[] }>("/v1/mcp/servers")
    ]);
    onChange(ext.extensions, mcp.servers);
  }
  async function refreshPermissions() {
    const [requests, grants] = await Promise.all([
      api<{ requests: PermissionRequest[] }>("/v1/permissions/requests?status=pending"),
      api<{ grants: PermissionGrant[] }>("/v1/permissions/grants")
    ]);
    setPermissionRequests(requests.requests);
    setPermissionGrants(grants.grants);
    return requests.requests;
  }
  function fail(reason: unknown) {
    const message = friendlyError(reason);
    setError(message);
    onError(message);
  }
  async function removeConfirmed() {
    if (!confirmDelete) return;
    const { kind, id } = confirmDelete;
    setConfirmDelete(null);
    try {
      if (kind === "mcp") await api(`/v1/mcp/servers/${encodeURIComponent(id)}`, { method: "DELETE" });
      else await api(`/v1/extensions/${encodeURIComponent(id)}`, { method: "DELETE" });
      await refresh();
    } catch (reason) { fail(reason); }
  }
  async function addMcp(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      await api("/v1/mcp/servers", { method: "POST", body: {
        name: mcpForm.name, transport: mcpForm.transport,
        command: mcpForm.transport === "stdio" ? mcpForm.command : undefined,
        args: mcpForm.transport === "stdio" ? mcpForm.args.split(",").map((item) => item.trim()).filter(Boolean) : undefined,
        cwd: mcpForm.transport === "stdio" ? mcpForm.cwd || undefined : undefined,
        env: mcpForm.transport === "stdio" ? mcpForm.env.split(",").map((item) => item.trim()).filter(Boolean) : undefined,
        url: mcpForm.transport !== "stdio" ? mcpForm.url : undefined,
        sandbox: mcpForm.transport === "stdio" ? mcpForm.sandbox : undefined,
        network: mcpForm.transport === "stdio" ? mcpForm.network : undefined,
        oauth: mcpForm.transport === "http" ? mcpForm.oauth : false,
        oauth_client_id: mcpForm.transport === "http" ? mcpForm.oauth_client_id : "",
        oauth_scopes: mcpForm.transport === "http" ? mcpForm.oauth_scopes.split(/[ ,]+/).filter(Boolean) : [],
        enabled: mcpForm.enabled,
        approved: mcpForm.approved,
      } });
      setMcpForm({ name: "", transport: "stdio", command: "", args: "", cwd: "", env: "", url: "", enabled: true, approved: false, sandbox: true, network: false, oauth: false, oauth_client_id: "", oauth_scopes: "" });
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function importMcp(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      await api("/v1/mcp/import", { method: "POST", body: { source_path: mcpImportPath } });
      setMcpImportPath("");
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function updateMcp(server: McpServer, patch: { enabled?: boolean; approved?: boolean }) {
    setBusy(true); setError("");
    try {
      await api(`/v1/mcp/servers/${encodeURIComponent(server.name)}`, { method: "PATCH", body: patch });
      if (patch.enabled === false || patch.approved === false) {
        setMcpTools((current) => { const next = { ...current }; delete next[server.name]; return next; });
      }
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function testMcp(server: McpServer) {
    setBusy(true); setError("");
    try {
      if (!server.approved) {
        await api(`/v1/mcp/servers/${encodeURIComponent(server.name)}`, {
          method: "PATCH", body: { approved: true, enabled: true },
        });
      }
      await api(`/v1/mcp/servers/${encodeURIComponent(server.name)}/connect`, { method: "POST" });
      const response = await api<{ tools: McpTool[] }>(`/v1/mcp/servers/${encodeURIComponent(server.name)}/tools`);
      setMcpTools((current) => ({ ...current, [server.name]: response.tools }));
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function authorizeMcp(server: McpServer) {
    setBusy(true); setError("");
    try {
      if (!window.zagent.oauthInfo || !window.zagent.openExternal) throw new Error("OAuth 需要 Electron 桌面端");
      const info = await window.zagent.oauthInfo();
      const result = await api<{ authorization_url: string }>(`/v1/mcp/servers/${encodeURIComponent(server.name)}/oauth/begin`, {
        method: "POST", body: { redirect_uri: info.redirectUri }
      });
      await window.zagent.openExternal(result.authorization_url);
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function connectExtension(extension: Extension) {
    setBusy(true); setError("");
    try {
      await finishExtensionConnection(extension);
    } catch (reason) {
      const requestId = permissionRequestId(reason);
      const requests = await refreshPermissions();
      const request = requests.find((item) => item.request_id === requestId);
      if (request) setHostPermission({ request, extension });
      else fail(reason);
    } finally { setBusy(false); }
  }
  async function finishExtensionConnection(extension: Extension) {
    await api(`/v1/extensions/${encodeURIComponent(extension.id)}/host/connect`, { method: "POST", body: {} });
    const response = await api<{ tools: McpTool[] }>(`/v1/extensions/${encodeURIComponent(extension.id)}/host/tools`);
    setExtensionTools((current) => ({ ...current, [extension.id]: response.tools }));
  }
  async function decideHostPermission(decision: "approved" | "denied", scope: "once" | "session") {
    if (!hostPermission || busy) return;
    const pending = hostPermission;
    setBusy(true); setError("");
    try {
      await api(`/v1/permissions/requests/${pending.request.request_id}/decision`, {
        method: "POST", body: { decision, scope },
      });
      setHostPermission(undefined);
      if (decision === "approved") await finishExtensionConnection(pending.extension);
      await refreshPermissions();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function decidePermission(request: PermissionRequest, decision: "approved" | "denied", scope: "once" | "session" | "always") {
    setBusy(true); setError("");
    try {
      await api(`/v1/permissions/requests/${request.request_id}/decision`, { method: "POST", body: { decision, scope } });
      await refreshPermissions();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function revokeGrant(grant: PermissionGrant) {
    try {
      await api(`/v1/permissions/grants/${grant.grant_id}`, { method: "DELETE" });
      await refreshPermissions();
    } catch (reason) { fail(reason); }
  }
  async function searchRegistry(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await api<{ servers: RegistryServer[] }>(`/v1/mcp/registry/servers?search=${encodeURIComponent(registryQuery)}&limit=20`);
      setRegistryServers(response.servers || []);
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function importRegistry(server: RegistryServer) {
    const name = server.server?.name || server.name;
    if (!name) return;
    setBusy(true); setError("");
    try {
      await api("/v1/mcp/registry/import", { method: "POST", body: { server_name: name, version: server.server?.version || "latest" } });
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function addExtension(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      await api("/v1/extensions", { method: "POST", body: {
        id: extForm.id, name: extForm.name, runtime: extForm.runtime,
        entry: extForm.entry || undefined, contributes: extForm.contributes,
      } });
      setExtForm({ id: "", name: "", runtime: "declarative", entry: "", contributes: ["tools"] });
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function importExtension(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      await api("/v1/extensions/import", { method: "POST", body: {
        source_path: importPath, enabled: importEnabled,
      } });
      setImportPath(""); setImportEnabled(false);
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  async function toggleExtension(extension: Extension) {
    setBusy(true); setError("");
    try {
      await api(`/v1/extensions/${encodeURIComponent(extension.id)}`, {
        method: "PATCH", body: { enabled: !extension.enabled },
      });
      await refresh();
    } catch (reason) { fail(reason); }
    finally { setBusy(false); }
  }
  function toggleContribution(value: string) {
    setExtForm((current) => ({
      ...current,
      contributes: current.contributes.includes(value)
        ? current.contributes.filter((item) => item !== value)
        : [...current.contributes, value],
    }));
  }

  return <Modal title="扩展与 MCP" onClose={onClose}><div className="settings">
    <section className="settings-section">
      <div className="settings-head"><h3>MCP Servers</h3></div>
      {mcpServers.length ? <div className="model-list">{mcpServers.map((server) =>
        <div key={server.name} className="integration-card">
          <div className="integration-row">
          <div className="model-info">
            <strong>{server.name}</strong>
            <small>{server.url ? server.url : `${server.command || ""} ${(server.args || []).join(" ")}`.trim()}</small>
          </div>
          <span className={`badge ${server.status === "connected" ? "badge-on" : "badge-off"}`}>{server.transport} · {server.status}</span>
          {server.transport !== "sse" && <button type="button" className="btn-ghost" disabled={busy} onClick={() => testMcp(server)}>连接并读取工具</button>}
          {server.transport === "http" && server.oauth && <button type="button" className="btn-ghost" disabled={busy} onClick={() => authorizeMcp(server)}>OAuth 授权</button>}
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => updateMcp(server, { approved: !server.approved, enabled: true })}>{server.approved ? "撤销授权" : "授权给 Agent"}</button>
          <button type="button" className="btn-danger" onClick={() => setConfirmDelete({ kind: "mcp", id: server.name, message: `删除 MCP server「${server.name}」？` })}>删除</button>
          </div>
          {mcpTools[server.name] && <div className="integration-tools"><strong>已发现 {mcpTools[server.name].length} 个工具</strong>{mcpTools[server.name].map((tool) => <code key={tool.name}>{tool.name}</code>)}</div>}
        </div>)}</div> : <p className="muted">尚未配置 MCP server，可在下方添加。</p>}
      <form className="settings-form" onSubmit={importMcp}>
        <div className="settings-head"><h3>导入本地 MCP 配置</h3></div>
        <div className="path-field"><span>配置文件</span><div className="path-row">
          <input aria-label="MCP 配置文件" value={mcpImportPath} onChange={(event) => setMcpImportPath(event.target.value)} placeholder="选择 *.json、*.mcpb 或 *.dxt" required />
          {window.zagent.selectMcpConfig && <button type="button" className="btn-ghost" onClick={async () => {
            const selected = await window.zagent.selectMcpConfig!();
            if (selected) setMcpImportPath(selected);
          }}>选择…</button>}
        </div></div>
        <p className="settings-hint">支持 Z-Agent、Claude Desktop 的 mcpServers、VS Code 的 servers，以及 MCPB/DXT 0.1–0.4 的 Node/Python/Binary Bundle；包内授权状态和环境变量值不会被信任，导入后必须由你显式授权。</p>
        <div className="form-actions"><button disabled={busy || !mcpImportPath}>安全导入 MCP</button></div>
      </form>
      <form className="settings-form" onSubmit={addMcp}>
        <div className="settings-head"><h3>添加 MCP Server</h3></div>
        <label>名称<input value={mcpForm.name} onChange={(event) => setMcpForm({ ...mcpForm, name: event.target.value })} placeholder="例如：files" /></label>
        <label>传输方式<select value={mcpForm.transport} onChange={(event) => setMcpForm({ ...mcpForm, transport: event.target.value })}>{MCP_TRANSPORTS.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
        {mcpForm.transport === "stdio"
          ? <>
              <label>启动命令<input value={mcpForm.command} onChange={(event) => setMcpForm({ ...mcpForm, command: event.target.value })} placeholder="例如：npx" /></label>
              <label>参数（逗号分隔）<input value={mcpForm.args} onChange={(event) => setMcpForm({ ...mcpForm, args: event.target.value })} placeholder="-y, @modelcontextprotocol/server-filesystem, /tmp" /></label>
              <label>工作目录（可选）<input value={mcpForm.cwd} onChange={(event) => setMcpForm({ ...mcpForm, cwd: event.target.value })} placeholder="/path/to/server" /></label>
              <label>允许传入的环境变量名（逗号分隔）<input value={mcpForm.env} onChange={(event) => setMcpForm({ ...mcpForm, env: event.target.value })} placeholder="DEEPSEEK_API_KEY" /></label>
            </>
          : <label>URL<input value={mcpForm.url} onChange={(event) => setMcpForm({ ...mcpForm, url: event.target.value })} placeholder="https://mcp.example.com/sse" /></label>}
        {mcpForm.transport === "stdio" && <>
          <label className="check-row"><input type="checkbox" checked={mcpForm.sandbox} onChange={(event) => setMcpForm({ ...mcpForm, sandbox: event.target.checked })} />启用 OS 沙箱（推荐，环境不支持时拒绝启动）</label>
          <label className="check-row"><input type="checkbox" checked={mcpForm.network} onChange={(event) => setMcpForm({ ...mcpForm, network: event.target.checked })} />允许子进程联网</label>
        </>}
        {mcpForm.transport === "http" && <>
          <label className="check-row"><input type="checkbox" checked={mcpForm.oauth} onChange={(event) => setMcpForm({ ...mcpForm, oauth: event.target.checked })} />使用 MCP OAuth</label>
          {mcpForm.oauth && <>
            <label>OAuth Client ID<input value={mcpForm.oauth_client_id} onChange={(event) => setMcpForm({ ...mcpForm, oauth_client_id: event.target.value })} /></label>
            <label>Scopes（空格分隔）<input value={mcpForm.oauth_scopes} onChange={(event) => setMcpForm({ ...mcpForm, oauth_scopes: event.target.value })} placeholder="mcp:tools" /></label>
          </>}
        </>}
        <label className="check-row"><input type="checkbox" checked={mcpForm.enabled} onChange={(event) => setMcpForm({ ...mcpForm, enabled: event.target.checked })} />启用</label>
        <label className="check-row"><input type="checkbox" checked={mcpForm.approved} onChange={(event) => setMcpForm({ ...mcpForm, approved: event.target.checked })} />允许启动该进程并将工具暴露给 Agent</label>
        <p className="settings-hint">stdio 与 Streamable HTTP 可真实执行；旧版 SSE 仅保留兼容配置。进程默认进入 OS 沙箱，工具每次调用都经过 Permission Broker。OAuth 使用 PKCE S256，令牌不会发送到 Renderer。</p>
        {error && <div className="settings-error" role="alert">{error}</div>}
        <div className="form-actions"><button disabled={busy}>添加 MCP</button></div>
      </form>
    </section>

    <section className="settings-section">
      <div className="settings-head"><h3>MCP Registry</h3></div>
      <form className="inline-search" onSubmit={searchRegistry}>
        <input aria-label="搜索 MCP Registry" value={registryQuery} onChange={(event) => setRegistryQuery(event.target.value)} placeholder="搜索官方 MCP Registry" />
        <button disabled={busy}>搜索</button>
      </form>
      {registryServers.map((item, index) => {
        const name = item.server?.name || item.name || `registry-${index}`;
        return <div className="integration-card" key={name}>
          <div className="integration-row"><div className="model-info"><strong>{name}</strong><small>{item.server?.description || item.description || item.server?.version || "官方 Registry 条目"}</small></div>
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => importRegistry(item)}>导入远程端点</button></div>
        </div>;
      })}
      <p className="settings-hint">Registry 导入始终保持“未授权”；只导入声明的 Streamable HTTP remote，不自动执行 npm/pip 安装脚本。</p>
    </section>

    <section className="settings-section">
      <div className="settings-head"><h3>Z-Agent Extensions</h3></div>
      {extensions.length ? <div className="model-list">{extensions.map((ext) =>
        <div key={ext.id} className="integration-card">
          <div className="integration-row">
          <div className="model-info">
            <strong>{ext.name || ext.id}</strong>
            <small>{ext.contributes.join("、") || "无贡献类型"} · 签名 {ext.signature_status || "unsigned"}{ext.package_sha256 ? ` · SHA ${ext.package_sha256.slice(0, 12)}` : ""}</small>
          </div>
          {(ext.runtime === "python" || ext.runtime === "node") && <button type="button" className="btn-ghost" disabled={busy || !ext.enabled} onClick={() => connectExtension(ext)}>启动独立 Host</button>}
          <button type="button" className="btn-ghost" disabled={busy} onClick={() => toggleExtension(ext)}>{ext.enabled ? "停用" : "启用"}</button>
          <button type="button" className="btn-danger" onClick={() => setConfirmDelete({ kind: "extension", id: ext.id, message: `删除扩展「${ext.id}」？` })}>删除</button>
          </div>
          {extensionTools[ext.id] && <div className="integration-tools"><strong>Host 提供 {extensionTools[ext.id].length} 个工具</strong>{extensionTools[ext.id].map((tool) => <code key={tool.name}>{tool.name}</code>)}</div>}
        </div>)}</div> : <p className="muted">尚未添加扩展，可在下方添加。</p>}
      <form className="settings-form" onSubmit={importExtension}>
        <div className="settings-head"><h3>导入扩展包</h3></div>
        <div className="path-field"><span>扩展根目录或 ZIP</span><div className="path-row">
          <input aria-label="扩展根目录或 ZIP" value={importPath} onChange={(event) => setImportPath(event.target.value)} placeholder="选择含 zagent.extension.json 的目录或 ZIP" required />
          {window.zagent.selectExtension && <button type="button" className="btn-ghost" onClick={async () => {
            const selected = await window.zagent.selectExtension!();
            if (selected) setImportPath(selected);
          }}>选择…</button>}
        </div></div>
        <label className="check-row"><input type="checkbox" checked={importEnabled} onChange={(event) => setImportEnabled(event.target.checked)} />导入后启用 manifest（可执行扩展仍不会在主进程直接运行）</label>
        <div className="form-actions"><button disabled={busy || !importPath}>安全导入</button></div>
      </form>
      <form className="settings-form" onSubmit={addExtension}>
        <div className="settings-head"><h3>创建开发用 manifest</h3></div>
        <label>扩展 ID<input value={extForm.id} onChange={(event) => setExtForm({ ...extForm, id: event.target.value })} placeholder="com.example.my-tool（小写字母/数字/./_/-）" /></label>
        <label>名称<input value={extForm.name} onChange={(event) => setExtForm({ ...extForm, name: event.target.value })} placeholder="可留空，默认使用 ID" /></label>
        <label>运行方式<select value={extForm.runtime} onChange={(event) => setExtForm({ ...extForm, runtime: event.target.value })}>{EXTENSION_RUNTIMES.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
        <label>入口文件（可选）<input value={extForm.entry} onChange={(event) => setExtForm({ ...extForm, entry: event.target.value })} placeholder="index.js（declarative 可留空）" /></label>
        <fieldset className="check-group"><legend>贡献类型</legend>{EXTENSION_CONTRIBUTIONS.map((item) =>
          <label key={item} className="check-row"><input type="checkbox" checked={extForm.contributes.includes(item)} onChange={() => toggleContribution(item)} />{item}</label>)}</fieldset>
        {error && <div className="settings-error" role="alert">{error}</div>}
        <div className="form-actions"><button disabled={busy}>添加扩展</button></div>
      </form>
    </section>

    <section className="settings-section">
      <div className="settings-head"><h3>Permission Broker</h3><button type="button" className="btn-ghost" onClick={() => refreshPermissions().catch(fail)}>刷新</button></div>
      {permissionRequests.length ? <div className="model-list">{permissionRequests.map((request) => <div key={request.request_id} className="integration-card">
        <div className="integration-row"><div className="model-info"><strong>{request.subject_type} · {request.subject_id}</strong><small>{request.action} · {JSON.stringify(request.details)}</small></div>
          <button type="button" className="btn-ghost" onClick={() => decidePermission(request, "approved", "once")}>仅这一次</button>
          <button type="button" className="btn-ghost" disabled={!request.session_id} title={request.session_id ? "允许当前会话" : "此请求不属于会话"} onClick={() => decidePermission(request, "approved", "session")}>本会话</button>
          <button type="button" className="btn-ghost" onClick={() => decidePermission(request, "approved", "always")}>始终允许</button>
          <button type="button" className="btn-danger" onClick={() => decidePermission(request, "denied", "once")}>拒绝</button>
        </div>
      </div>)}</div> : <p className="muted">当前没有待处理授权。扩展启动和工具执行默认拒绝，并在此逐次确认。</p>}
      {!!permissionGrants.length && <Collapsible summary={`有效授权 ${permissionGrants.length} 项`}><div className="model-list">{permissionGrants.map((grant) => <div key={grant.grant_id} className="model-item plain"><div className="model-info"><strong>{grant.subject_type} · {grant.subject_id}</strong><small>{grant.action} · {grant.scope}</small></div><button type="button" className="btn-danger" onClick={() => revokeGrant(grant)}>撤销</button></div>)}</div></Collapsible>}
    </section>
    {confirmDelete && <ConfirmDialog message={confirmDelete.message} confirmText="确认删除"
      onCancel={() => setConfirmDelete(null)} onConfirm={removeConfirmed} />}
  </div>{hostPermission && <PermissionDialog request={hostPermission.request} busy={busy} onDecision={decideHostPermission} />}</Modal>;
}

function permissionRequestId(reason: unknown) {
  const text = reason instanceof Error ? reason.message : String(reason);
  return text.match(/permission_request_id=([A-Za-z0-9_-]+)/)?.[1];
}

export default App;
