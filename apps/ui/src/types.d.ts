interface StreamEvent {
  type: "content" | "tool_call" | "done" | "error" | "cancelled";
  text?: string;
  result?: unknown;
  message?: string;
  checkpoint?: { checkpoint_id: string; reason: string; state: Record<string, unknown> };
}

interface ZAgentBridge {
  request<T>(path: string, options?: { method?: string; body?: unknown }): Promise<T>;
  requestStream?<T>(path: string, options?: { method?: string; body?: unknown }, onEvent?: (event: StreamEvent) => void): Promise<{ result?: T; message?: string; type?: string; checkpoint?: { checkpoint_id: string; reason: string; state: Record<string, unknown> } }>;
  cancelStream?(): Promise<{ cancelled: boolean }>;
  selectFolder?(): Promise<string | null>;
  selectExtension?(): Promise<string | null>;
  platform: string;
}

interface Window { zagent: ZAgentBridge; }
