interface StreamEvent {
  type: "content" | "tool_call" | "done" | "error" | "cancelled";
  text?: string;
  result?: unknown;
  message?: string;
}

interface ZAgentBridge {
  request<T>(path: string, options?: { method?: string; body?: unknown }): Promise<T>;
  requestStream?<T>(path: string, options?: { method?: string; body?: unknown }, onEvent?: (event: StreamEvent) => void): Promise<{ result?: T; message?: string; type?: string }>;
  cancelStream?(): Promise<{ cancelled: boolean }>;
  selectFolder?(): Promise<string | null>;
  platform: string;
}

interface Window { zagent: ZAgentBridge; }
