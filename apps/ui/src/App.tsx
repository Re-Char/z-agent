import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";

type Session = { session_id: string; title: string; updated_at: string; event_count: number };
type Event = { event_id: string; sequence: number; timestamp: string; kind: string; role: string; payload: unknown; token_estimate: number; tool_name?: string };
type ContextStatus = { stats: { count: number; tokens: number }; working_set: { tokens: number; budget: number; included_event_ids: string[]; pinned_event_ids: string[] }; latest_archive?: { archive_id: string; state: unknown } };
type ModelConfig = { provider: string; model: string; base_url: string; context_window: number; hard_limit_ratio: number };
type AppConfig = { locale: string; model: ModelConfig };

const api = <T,>(path: string, options?: { method?: string; body?: unknown }) => window.zagent.request<T>(path, options);

function payloadText(payload: unknown) {
  return typeof payload === "string" ? payload : JSON.stringify(payload, null, 2);
}

function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string>();
  const [events, setEvents] = useState<Event[]>([]);
  const [context, setContext] = useState<ContextStatus>();
  const [config, setConfig] = useState<AppConfig>();
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [extensionsOpen, setExtensionsOpen] = useState(false);
  const [extensions, setExtensions] = useState<Array<Record<string, unknown>>>([]);
  const [mcpServers, setMcpServers] = useState<Array<Record<string, unknown>>>([]);

  const refreshSessions = useCallback(async () => {
    const response = await api<{ sessions: Session[] }>("/v1/sessions");
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
    Promise.all([refreshSessions(), api<AppConfig>("/v1/config").then(setConfig)]).catch((reason) => setError(String(reason)));
  }, [refreshSessions]);
  useEffect(() => { refreshActive(activeId).catch((reason) => setError(String(reason))); }, [activeId, refreshActive]);

  async function createSession() {
    const session = await api<Session>("/v1/sessions", { method: "POST", body: { title: "新任务" } });
    await refreshSessions();
    setActiveId(session.session_id);
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!input.trim() || busy) return;
    let sessionId = activeId;
    if (!sessionId) {
      const session = await api<Session>("/v1/sessions", { method: "POST", body: { title: input.slice(0, 24) } });
      sessionId = session.session_id;
      setActiveId(sessionId);
    }
    const content = input;
    setInput("");
    setBusy(true);
    setError("");
    try {
      await api(`/v1/sessions/${sessionId}/messages`, { method: "POST", body: { content } });
      await Promise.all([refreshActive(sessionId), refreshSessions()]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(false); }
  }

  async function openExtensions() {
    const [ext, mcp] = await Promise.all([
      api<{ extensions: Array<Record<string, unknown>> }>("/v1/extensions"),
      api<{ servers: Array<Record<string, unknown>> }>("/v1/mcp/servers")
    ]);
    setExtensions(ext.extensions); setMcpServers(mcp.servers); setExtensionsOpen(true);
  }

  const ratio = useMemo(() => context ? Math.min(100, Math.round(context.working_set.tokens / context.working_set.budget * 100)) : 0, [context]);

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">Z</span><div><strong>Z-Agent</strong><small>中文长程智能体</small></div></div>
      <button className="new-task" onClick={createSession}>＋ 新建任务</button>
      <div className="section-label">最近任务</div>
      <nav className="session-list">{sessions.map((session) =>
        <button key={session.session_id} className={activeId === session.session_id ? "session active" : "session"} onClick={() => setActiveId(session.session_id)}>
          <span>{session.title}</span><small>{session.event_count} 个事件</small>
        </button>)}</nav>
      <div className="sidebar-actions">
        <button onClick={openExtensions}>扩展与 MCP</button>
        <button onClick={() => setSettingsOpen(true)}>模型设置</button>
      </div>
    </aside>

    <main className="workspace">
      <header className="topbar"><div><strong>{sessions.find((item) => item.session_id === activeId)?.title || "开始一个新任务"}</strong><span className="model-pill">{config?.model.provider} · {config?.model.model}</span></div><span className="status-dot">核心在线</span></header>
      <section className="timeline">
        {!events.length && <div className="empty-state"><div className="orb">知</div><h1>把复杂任务交给一个<br />记得住过程的智能体</h1><p>所有上下文都可追踪、可归档、可恢复。中文与工具参数各自保持准确。</p></div>}
        {events.map((item) => <article key={item.event_id} className={`event ${item.role}`}>
          <div className="event-meta"><span>{item.role === "user" ? "你" : item.role === "assistant" ? "Z-Agent" : item.kind}</span><code>#{item.sequence} · {item.event_id.slice(-8)}</code></div>
          <pre>{payloadText(item.payload)}</pre>
          {item.tool_name && <span className="tool-tag">{item.tool_name}</span>}
        </article>)}
        {busy && <div className="thinking"><i></i><i></i><i></i><span>正在组织上下文…</span></div>}
      </section>
      {error && <div className="error-banner">{error}</div>}
      <form className="composer" onSubmit={send}><textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="描述你的任务，Enter 发送，Shift+Enter 换行" onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} /><button disabled={busy || !input.trim()}>发送</button></form>
    </main>

    <aside className="inspector">
      <div className="inspector-title"><strong>上下文检查器</strong><span>LIVE</span></div>
      <div className="meter"><div><span>工作集</span><strong>{context?.working_set.tokens || 0} / {context?.working_set.budget || 0}</strong></div><div className="meter-track"><i style={{ width: `${ratio}%` }} /></div><small>已使用 {ratio}% · 原始事件不会被覆盖</small></div>
      <div className="stat-grid"><div><strong>{context?.stats.count || 0}</strong><span>事件</span></div><div><strong>{context?.working_set.pinned_event_ids.length || 0}</strong><span>固定证据</span></div></div>
      <div className="inspector-section"><h3>当前工作集</h3>{context?.working_set.included_event_ids.slice(-10).reverse().map((id) => <code key={id}>{id.slice(-12)}</code>) || <p>暂无事件</p>}</div>
      <div className="inspector-section"><h3>最近归档</h3>{context?.latest_archive ? <><code>{context.latest_archive.archive_id}</code><pre>{JSON.stringify(context.latest_archive.state, null, 2)}</pre></> : <p>Agent 完成阶段后会在这里留下可展开的任务状态。</p>}</div>
    </aside>

    {settingsOpen && config && <Settings config={config} onClose={() => setSettingsOpen(false)} onSaved={(next) => { setConfig(next); setSettingsOpen(false); }} />}
    {extensionsOpen && <Modal title="扩展与 MCP" onClose={() => setExtensionsOpen(false)}><h4>Z-Agent Extensions</h4>{extensions.length ? extensions.map((item) => <pre key={String(item.id)}>{JSON.stringify(item, null, 2)}</pre>) : <p className="muted">尚未发现扩展。可放入 ~/.zagent/extensions/&lt;id&gt;/。</p>}<h4>MCP Servers</h4>{mcpServers.length ? mcpServers.map((item) => <pre key={String(item.name)}>{JSON.stringify(item, null, 2)}</pre>) : <p className="muted">尚未配置 MCP server。</p>}</Modal>}
  </div>;
}

function Modal({ title, onClose, children }: { title: string; onClose(): void; children: ReactNode }) {
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}><section className="modal"><header><h2>{title}</h2><button onClick={onClose}>×</button></header>{children}</section></div>;
}

function Settings({ config, onClose, onSaved }: { config: AppConfig; onClose(): void; onSaved(value: AppConfig): void }) {
  const [model, setModel] = useState(config.model);
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault(); setSaving(true);
    try { onSaved(await api<AppConfig>("/v1/config/model", { method: "POST", body: { ...model, api_key: apiKey || undefined } })); }
    finally { setSaving(false); }
  }
  return <Modal title="模型设置" onClose={onClose}><form className="settings" onSubmit={save}>
    <label>适配器<select value={model.provider} onChange={(event) => setModel({ ...model, provider: event.target.value })}><option value="echo">本地演示</option><option value="openai_compatible">OpenAI Compatible</option><option value="qwen">通义千问</option><option value="deepseek">DeepSeek</option><option value="glm">智谱 GLM</option><option value="kimi">Kimi</option><option value="minimax">MiniMax</option></select></label>
    <label>模型名称<input value={model.model} onChange={(event) => setModel({ ...model, model: event.target.value })} /></label>
    <label>API Base URL<input value={model.base_url} onChange={(event) => setModel({ ...model, base_url: event.target.value })} placeholder="https://.../v1" /></label>
    <label>API Key<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="不填写则保持现有密钥" /></label>
    <label>上下文窗口<input type="number" value={model.context_window} onChange={(event) => setModel({ ...model, context_window: Number(event.target.value) })} /></label>
    <div className="form-actions"><button type="button" onClick={onClose}>取消</button><button disabled={saving}>保存并切换</button></div>
  </form></Modal>;
}

export default App;
