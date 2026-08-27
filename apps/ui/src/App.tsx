import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Markdown } from "./Markdown";

type Session = { session_id: string; title: string; updated_at: string; event_count: number };
type Event = { event_id: string; sequence: number; timestamp: string; kind: string; role: string; payload: unknown; token_estimate: number; tool_name?: string };
type ContextStatus = { stats: { count: number; tokens: number }; working_set: { tokens: number; budget: number; included_event_ids: string[]; pinned_event_ids: string[]; dropped_pinned_ids: string[]; pinned_tokens: number }; latest_archive?: { archive_id: string; state: unknown }; warning?: string | null; pinned_tokens: number };
type ModelConfig = { id: string; name: string; provider: string; model: string; base_url: string; context_window: number; soft_limit_ratio: number; hard_limit_ratio: number };
type AppConfig = { locale: string; model: ModelConfig; models: ModelConfig[]; active_model_id: string };
type TokenStats = { total_tokens: number; completion_tokens: number; cache_hit_tokens: number; cache_miss_tokens: number; cache_hit_rate: number; elapsed_seconds: number; tokens_per_second: number };
type McpServer = { name: string; transport: string; enabled: boolean; command?: string | null; args?: string[] | null; url?: string | null; status: string };
type Extension = { id: string; name: string; version: string; runtime: string; entry: string | null; contributes: string[]; permissions: string[]; enabled: boolean; status: string };
type Workspace = { workspace_id: string; name: string; path: string; session_count: number };

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
  return typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
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
  const [error, setError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [extensionsOpen, setExtensionsOpen] = useState(false);
  const [extensions, setExtensions] = useState<Extension[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [workspaceDialogOpen, setWorkspaceDialogOpen] = useState(false);
  const [workspaceEditOpen, setWorkspaceEditOpen] = useState(false);
  const [streaming, setStreaming] = useState<{ sessionId: string; text: string } | null>(null);

  const refreshWorkspaces = useCallback(async () => {
    const response = await api<{ workspaces: Workspace[] }>("/v1/workspaces");
    setWorkspaces(response.workspaces);
    setActiveWorkspaceId((current) => current || response.workspaces[0]?.workspace_id);
    return response.workspaces;
  }, []);

  const refreshSessions = useCallback(async (workspaceId?: string) => {
    const query = workspaceId ? `?workspace_id=${workspaceId}` : "";
    const response = await api<{ sessions: Session[] }>(`/v1/sessions${query}`);
    setSessions(response.sessions);
    setActiveId((current) => current || response.sessions[0]?.session_id);
  }, []);

  const refreshActive = useCallback(async (sessionId?: string) => {
    if (!sessionId) return;
    const [eventData, contextData] = await Promise.all([
      api<{ events: Event[] }>(`/v1/sessions/${sessionId}/events`),
      api<ContextStatus>(`/v1/sessions/${sessionId}/context`)
    ]);
    setEvents(eventData.events);
    setContext(contextData);
  }, []);

  useEffect(() => {
    Promise.all([refreshWorkspaces(), api<AppConfig>("/v1/config").then(setConfig)]).catch((reason) => setError(friendlyError(reason)));
  }, [refreshWorkspaces]);
  useEffect(() => {
    if (activeWorkspaceId) refreshSessions(activeWorkspaceId).catch((reason) => setError(friendlyError(reason)));
  }, [activeWorkspaceId, refreshSessions]);
  useEffect(() => { refreshActive(activeId).catch((reason) => setError(friendlyError(reason))); }, [activeId, refreshActive]);

  // --- auto-scroll: follow the newest content unless the user scrolled up ---
  const timelineRef = useRef<HTMLElement>(null);
  const stickToBottom = useRef(true);
  const prevActiveId = useRef<string | undefined>(undefined);
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
    const session = await api<Session>("/v1/sessions", { method: "POST", body: { title: "新任务", workspace_id: activeWorkspaceId } });
    await refreshSessions(activeWorkspaceId);
    setActiveId(session.session_id);
  }

  async function createWorkspace(workspace: Workspace) {
    setWorkspaceDialogOpen(false);
    await refreshWorkspaces();
    setActiveWorkspaceId(workspace.workspace_id);
    setActiveId(undefined);
  }

  async function switchWorkspace(workspaceId: string) {
    setActiveWorkspaceId(workspaceId);
    setActiveId(undefined);
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!input.trim() || busy) return;
    let sessionId = activeId;
    if (!sessionId) {
      const session = await api<Session>("/v1/sessions", { method: "POST", body: { title: input.slice(0, 24), workspace_id: activeWorkspaceId } });
      sessionId = session.session_id;
      setActiveId(sessionId);
    }
    const content = input;
    setInput("");
    setBusy(true);
    setError("");
    // Optimistic render: show the user's message immediately.
    const nextSequence = events.reduce((max, item) => Math.max(max, item.sequence), 0) + 1;
    const optimistic: Event = {
      event_id: `pending-${Date.now()}`, sequence: nextSequence,
      timestamp: new Date().toISOString(), kind: "message", role: "user",
      payload: content, token_estimate: 0,
    };
    setEvents((current) => [...current, optimistic]);
    try {
      if (window.zagent.requestStream) {
        // Streaming path: reply text appears progressively while the model thinks.
        let reply = "";
        const outcome = await window.zagent.requestStream<{ stats: TokenStats }>(
          `/v1/sessions/${sessionId}/messages/stream`,
          { method: "POST", body: { content } },
          (streamEvent) => {
            if (streamEvent.type === "content") {
              reply += streamEvent.text || "";
              setStreaming({ sessionId, text: reply });
            }
          }
        );
        if (outcome?.message) throw new Error(outcome.message);
        if (outcome?.result && (outcome.result as { stats?: TokenStats }).stats) {
          setLastStats((outcome.result as { stats: TokenStats }).stats);
        }
      } else {
        const result = await api<{ stats: TokenStats }>(`/v1/sessions/${sessionId}/messages`, { method: "POST", body: { content } });
        setLastStats(result.stats);
      }
      await Promise.all([refreshActive(sessionId), refreshSessions()]);
    } catch (reason) {
      setEvents((current) => current.filter((item) => item.event_id !== optimistic.event_id));
      setError(friendlyError(reason));
    } finally {
      setBusy(false);
      setStreaming(null);
    }
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
  const softRatioPct = budget > 0 && softLimit > 0 ? softLimit / budget * 100 : 0;

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">Z</span><div><strong>Z-Agent</strong><small>中文长程智能体</small></div></div>
      <div className="workspace-bar">
        <select className="workspace-switcher" value={activeWorkspaceId || ""} onChange={(event) => switchWorkspace(event.target.value)} title="工作区 = agent 的安全边界（可访问目录）">
          {workspaces.map((item) => <option key={item.workspace_id} value={item.workspace_id}>{item.name}{item.path ? ` · ${item.path}` : " · 未设置路径"}</option>)}
        </select>
        <button className="workspace-add" onClick={() => setWorkspaceDialogOpen(true)} title="新建工作区">＋</button>
        <button className="workspace-edit" onClick={() => setWorkspaceEditOpen(true)} title="编辑当前工作区的名称与路径">✎</button>
      </div>
      <button className="new-task" onClick={createSession}>＋ 新建对话</button>
      <div className="section-label">最近任务</div>
      <nav className="session-list">{sessions.map((session) =>
        <button key={session.session_id} className={activeId === session.session_id ? "session active" : "session"} onClick={() => setActiveId(session.session_id)}>
          <span>{session.title}</span><small>{session.event_count} 个事件 · {formatRelativeTime(session.updated_at)}</small>
        </button>)}</nav>
      <div className="sidebar-actions">
        <button onClick={openExtensions}>扩展与 MCP</button>
        <button onClick={() => setSettingsOpen(true)}>模型设置</button>
      </div>
    </aside>

    <main className="workspace">
      <header className="topbar">
        <div>
          <strong>{sessions.find((item) => item.session_id === activeId)?.title || "开始一个新任务"}</strong>
          {config && config.models && config.models.length > 1
            ? <select className="model-switcher" value={config.active_model_id} onChange={(event) => switchModel(event.target.value)} title="切换模型">
                {config.models.map((model) => <option key={model.id} value={model.id}>{modelLabel(model)}</option>)}
              </select>
            : <span className="model-pill">{config?.model.provider} · {config?.model.model}</span>}
        </div>
        <span className="status-dot">核心在线</span>
      </header>
      <section className="timeline" ref={timelineRef} onScroll={handleTimelineScroll}>
        {!events.length && <div className="empty-state"><div className="orb">知</div><h1>把复杂任务交给一个<br />记得住过程的智能体</h1><p>所有上下文都可追踪、可归档、可恢复。中文与工具参数各自保持准确。</p></div>}
        {events.map((item) => <EventCard key={item.event_id} item={item} pinned={pinnedIds.has(item.event_id)} onTogglePin={togglePin} showPin={!!context} />)}
        {busy && streaming && streaming.sessionId === activeId && <article className="event assistant streaming-bubble">
          <div className="event-meta"><span>Z-Agent</span><code>正在输出…</code></div>
          <Markdown text={streaming.text || "…"} />
          <span className="streaming-caret" />
        </article>}
        {busy && <div className="thinking"><i></i><i></i><i></i><span>正在组织上下文…</span></div>}
      </section>
      {error && <div className="error-banner">{error}</div>}
      <form className="composer" onSubmit={send}><textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="描述你的任务，Enter 发送，Shift+Enter 换行" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><button disabled={busy || !input.trim()}>发送</button></form>
    </main>

    <aside className="inspector">
      <div className="inspector-title"><strong>上下文检查器</strong><span>LIVE</span></div>
      {context?.warning && <div className="context-warning" role="alert">{context.warning}</div>}
      <div className="meter" title={`工作集 = 本次发送给模型的内容（系统提示 + 最近事件 + 固定证据），受预算限制。预算 = 上下文窗口 × 硬上限比例（当前 ${budget.toLocaleString()} tokens）。达到软上限（${softLimit.toLocaleString()}）后新事件可能被挤出。`}>
        <div><span>工作集占用</span><strong>{context?.working_set.tokens ? context.working_set.tokens.toLocaleString() : 0} / {budget.toLocaleString()}</strong></div>
        <div className="meter-track">
          {softRatioPct > 0 && softRatioPct < 100 && <i className="meter-soft" style={{ left: `${softRatioPct}%` }} title={`软上限 ${softLimit.toLocaleString()} tokens`} />}
          <i className="meter-fill" style={{ width: ratio > 0 ? `max(${ratio}%, 4px)` : "0%" }} />
        </div>
        <small>已使用 {ratioText}% · 软上限 {Math.round((config?.model.soft_limit_ratio || 0) * 100)}%（{softLimit.toLocaleString()} tokens）{context?.working_set.pinned_tokens ? ` · 固定 ${context.working_set.pinned_tokens.toLocaleString()}` : ""}</small>
      </div>
      <div className="stat-grid">
        <div title="事件 = 会话中的每一条记录：你的消息、模型回复、工具调用与结果、归档摘要等"><strong>{context?.stats.count || 0}</strong><span>事件</span></div>
        <div title="固定证据 = 被你手动固定的关键事件，始终保留在工作集中，不会被预算挤出"><strong>{pinnedIds.size}</strong><span>固定证据</span></div>
        <div title={`缓存命中率 = 命中缓存 token / (命中 + 未命中)。命中越多，首 token 延迟与费用越低（最近一次任务）`}><strong>{lastStats ? `${lastStats.cache_hit_rate}%` : "—"}</strong><span>缓存命中</span></div>
        <div title={`生成速度 = 完成任务生成的总 token 数 ÷ 总耗时（最近一次任务）`}><strong>{lastStats ? lastStats.tokens_per_second.toFixed(1) : "—"}</strong><span>tok/s</span></div>
      </div>
      {lastStats && <div className="inspector-section"><h3>最近任务</h3><p>{lastStats.total_tokens.toLocaleString()} tokens · 生成 {lastStats.completion_tokens.toLocaleString()} · 缓存 {lastStats.cache_hit_tokens.toLocaleString()}/{lastStats.cache_miss_tokens.toLocaleString()} · 耗时 {lastStats.elapsed_seconds.toFixed(1)}s</p></div>}
      <div className="inspector-section"><h3>当前工作集</h3>
        {context?.working_set.included_event_ids.slice(-10).reverse().map((id) => {
          const event = events.find((item) => item.event_id === id);
          const pinned = pinnedIds.has(id);
          const preview = event ? payloadText(event.payload).replace(/\s+/g, " ").slice(0, 42) : "";
          return <div key={id} className="ws-item">
            <span className={`ws-dot ${pinned ? "pinned" : ""}`} title={pinned ? "已固定为证据" : "普通事件"}>{pinned ? "📌" : "·"}</span>
            <code>{id.slice(-12)}</code>
            {preview && <span className="ws-preview">{preview}</span>}
            {pinned && event && <button className="ws-unpin" onClick={() => togglePin(event)} title="取消固定（点击后该事件可被预算清理）">取消</button>}
          </div>;
        }) || <p>暂无事件</p>}
      </div>
      <div className="inspector-section"><h3>最近归档</h3>{context?.latest_archive
        ? <div className="archive-card">
            <code>{context.latest_archive.archive_id}</code>
            <pre>{JSON.stringify(context.latest_archive.state, null, 2)}</pre>
            <p>归档摘要已注入系统提示词，模型在后续轮次仍能引用该阶段状态。</p>
          </div>
        : <p>模型在完成任务阶段后会调用归档工具，在这里留下任务状态摘要。你也可以通过对话要求"归档当前阶段"。</p>}</div>
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
  </div>;
}

function Modal({ title, onClose, children }: { title: string; onClose(): void; children: ReactNode }) {
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><section className="modal"><header><h2>{title}</h2><button onClick={onClose}>×</button></header>{children}</section></div>;
}

function Collapsible({ summary, children, defaultOpen }: { summary: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(!!defaultOpen);
  return <div className={`collapsible${open ? " open" : ""}`}>
    <button type="button" className="collapsible-summary" onClick={() => setOpen(!open)}><span className="chevron">{open ? "▾" : "▸"}</span>{summary}</button>
    {open && <div className="collapsible-body">{children}</div>}
  </div>;
}

function EventCard({ item, pinned, onTogglePin, showPin }: {
  item: Event; pinned: boolean; onTogglePin(item: Event): void; showPin: boolean;
}) {
  const isToolCallEvent = item.kind === "assistant_tool_calls";
  const isToolResult = item.role === "tool";
  const payload = item.payload;
  const toolCallPayload = isToolCallEvent && typeof payload === "object" && payload !== null ? payload as { content?: string; tool_calls?: Array<{ call_id: string; name: string; arguments: unknown }>; reasoning_content?: string } : null;

  function renderBody() {
    if (toolCallPayload) {
      return <div className="tool-event-body">
        {toolCallPayload.content ? <Markdown text={toolCallPayload.content} /> : null}
        {(toolCallPayload.tool_calls || []).map((call) =>
          <Collapsible key={call.call_id} defaultOpen={false}
            summary={<span className="tool-line"><span className="tool-line-name">🔧 {call.name}</span>
              <span className="tool-line-args">{safeArgsSummary(call.arguments)}</span></span>}>
            <pre>{JSON.stringify(call.arguments, null, 2)}</pre>
          </Collapsible>)}
        {toolCallPayload.reasoning_content
          ? <Collapsible summary={<span className="tool-line thinking-line">🧠 思考过程（{toolCallPayload.reasoning_content.length} 字）</span>}>
              <div className="thinking-text">{toolCallPayload.reasoning_content}</div>
            </Collapsible>
          : null}
      </div>;
    }
    if (isToolResult) {
      const summary = typeof payload === "string" ? payload.replace(/\s+/g, " ").slice(0, 60) : JSON.stringify(payload).slice(0, 80);
      return <Collapsible summary={<span className="tool-line"><span className="tool-line-name">⚙️ {item.tool_name || "tool_result"}</span><span className="tool-line-args">{summary}</span></span>}>
        <pre>{payloadText(payload)}</pre>
      </Collapsible>;
    }
    if (item.role === "assistant" && typeof item.payload === "string") return <Markdown text={item.payload} />;
    return <pre>{payloadText(item.payload)}</pre>;
  }

  return <article className={`event ${item.role}${pinned ? " pinned" : ""}`}>
    <div className="event-meta"><span>{item.role === "user" ? "你" : item.role === "assistant" ? "Z-Agent" : item.kind}</span><code>#{item.sequence} · {formatClockTime(item.timestamp)} · {item.event_id.slice(-8)}</code></div>
    {renderBody()}
    <div className="event-foot">
      {item.tool_name && <span className="tool-tag">{item.tool_name}</span>}
      {showPin && item.role !== "system" && <button className={pinned ? "pin-btn active" : "pin-btn"} onClick={() => onTogglePin(item)} title={pinned ? "取消固定：该事件之后可被工作集预算清理" : "固定为证据：即使会话变长、事件超出工作集预算，这条也始终会发给模型，不会被挤掉"}>
        {pinned ? "已固定" : "固定"}
      </button>}
    </div>
  </article>;
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
    <p className="settings-hint">工作区路径 = agent 的安全边界：文件工具只能读取该目录。设置后 agent 才能阅读你的项目文件。</p>
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
    <p className="settings-hint">工作区 = agent 的安全边界：未来 agent 的代码与文件工具只能访问该工作区目录内的内容。每个工作区有独立的对话列表。</p>
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
  const [mcpForm, setMcpForm] = useState({ name: "", transport: "stdio", command: "", args: "", url: "", enabled: true });
  const [extForm, setExtForm] = useState({ id: "", name: "", runtime: "declarative", entry: "", contributes: ["tools"] as string[] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<{ message: string; kind: "mcp" | "extension"; id: string } | null>(null);

  async function refresh() {
    const [ext, mcp] = await Promise.all([
      api<{ extensions: Extension[] }>("/v1/extensions"),
      api<{ servers: McpServer[] }>("/v1/mcp/servers")
    ]);
    onChange(ext.extensions, mcp.servers);
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
        url: mcpForm.transport !== "stdio" ? mcpForm.url : undefined,
        enabled: mcpForm.enabled,
      } });
      setMcpForm({ name: "", transport: "stdio", command: "", args: "", url: "", enabled: true });
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
        <div key={server.name} className="model-item plain">
          <div className="model-info">
            <strong>{server.name}</strong>
            <small>{server.url ? server.url : `${server.command || ""} ${(server.args || []).join(" ")}`.trim()}</small>
          </div>
          <span className={`badge ${server.enabled ? "badge-on" : "badge-off"}`}>{server.transport}{server.enabled ? " · 启用" : " · 停用"}</span>
          <button type="button" className="btn-danger" onClick={() => setConfirmDelete({ kind: "mcp", id: server.name, message: `删除 MCP server「${server.name}」？` })}>删除</button>
        </div>)}</div> : <p className="muted">尚未配置 MCP server，可在下方添加。</p>}
      <form className="settings-form" onSubmit={addMcp}>
        <div className="settings-head"><h3>添加 MCP Server</h3></div>
        <label>名称<input value={mcpForm.name} onChange={(event) => setMcpForm({ ...mcpForm, name: event.target.value })} placeholder="例如：files" /></label>
        <label>传输方式<select value={mcpForm.transport} onChange={(event) => setMcpForm({ ...mcpForm, transport: event.target.value })}>{MCP_TRANSPORTS.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
        {mcpForm.transport === "stdio"
          ? <>
              <label>启动命令<input value={mcpForm.command} onChange={(event) => setMcpForm({ ...mcpForm, command: event.target.value })} placeholder="例如：npx" /></label>
              <label>参数（逗号分隔）<input value={mcpForm.args} onChange={(event) => setMcpForm({ ...mcpForm, args: event.target.value })} placeholder="-y, @modelcontextprotocol/server-filesystem, /tmp" /></label>
            </>
          : <label>URL<input value={mcpForm.url} onChange={(event) => setMcpForm({ ...mcpForm, url: event.target.value })} placeholder="https://mcp.example.com/sse" /></label>}
        <label className="check-row"><input type="checkbox" checked={mcpForm.enabled} onChange={(event) => setMcpForm({ ...mcpForm, enabled: event.target.checked })} />启用</label>
        {error && <div className="settings-error" role="alert">{error}</div>}
        <div className="form-actions"><button disabled={busy}>添加 MCP</button></div>
      </form>
    </section>

    <section className="settings-section">
      <div className="settings-head"><h3>Z-Agent Extensions</h3></div>
      {extensions.length ? <div className="model-list">{extensions.map((ext) =>
        <div key={ext.id} className="model-item plain">
          <div className="model-info">
            <strong>{ext.name || ext.id}</strong>
            <small>{ext.contributes.join("、") || "无贡献类型"}{ext.entry ? ` · 入口 ${ext.entry}` : ""}</small>
          </div>
          <span className={`badge ${ext.enabled ? "badge-on" : "badge-off"}`}>{ext.runtime}{ext.enabled ? " · 启用" : " · 停用"}</span>
          <button type="button" className="btn-danger" onClick={() => setConfirmDelete({ kind: "extension", id: ext.id, message: `删除扩展「${ext.id}」？` })}>删除</button>
        </div>)}</div> : <p className="muted">尚未添加扩展，可在下方添加。</p>}
      <form className="settings-form" onSubmit={addExtension}>
        <div className="settings-head"><h3>添加扩展</h3></div>
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
    {confirmDelete && <ConfirmDialog message={confirmDelete.message} confirmText="确认删除"
      onCancel={() => setConfirmDelete(null)} onConfirm={removeConfirmed} />}
  </div></Modal>;
}

export default App;
