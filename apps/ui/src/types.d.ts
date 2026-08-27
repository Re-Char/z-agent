interface StreamEvent {
  type: "content" | "reasoning" | "tool_call" | "done" | "error";
  text?: string;
  result?: unknown;
  message?: string;
}

interface ZAgentBridge {
  request<T>(path: string, options?: { method?: string; body?: unknown }): Promise<T>;
  requestStream?<T>(path: string, options?: { method?: string; body?: unknown }, onEvent?: (event: StreamEvent) => void): Promise<{ result?: T; message?: string }>;
  selectFolder?(): Promise<string | null>;
  platform: string;
}

interface Window { zagent: ZAgentBridge; }
