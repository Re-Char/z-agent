/// <reference types="vite/client" />

interface StreamEvent {
  type: "content" | "tool_call" | "tool_result" | "permission_required" | "status" | "done" | "error" | "cancelled";
  text?: string;
  name?: string;
  ok?: boolean;
  round?: number;
  result?: unknown;
  message?: string;
  detail?: string;
  request?: {
    request_id: string;
    session_id?: string | null;
    subject_type: string;
    subject_id: string;
    action: string;
    details: Record<string, unknown>;
    status: string;
    created_at: string;
  };
  checkpoint?: { checkpoint_id: string; reason: string; state: Record<string, unknown> };
}

interface ZAgentBridge {
  request<T>(path: string, options?: { method?: string; body?: unknown }): Promise<T>;
  requestStream?<T>(path: string, options?: { method?: string; body?: unknown }, onEvent?: (event: StreamEvent) => void): Promise<{ result?: T; message?: string; type?: string; checkpoint?: { checkpoint_id: string; reason: string; state: Record<string, unknown> } }>;
  cancelStream?(): Promise<{ cancelled: boolean }>;
  selectFolder?(): Promise<string | null>;
  selectExtension?(): Promise<string | null>;
  selectMcpConfig?(): Promise<string | null>;
  saveJson?(suggestedName: string, content: string): Promise<string | null>;
  oauthInfo?(): Promise<{ redirectUri: string }>;
  onCoreStatus?(callback: (status: { status: "online" | "recovering" | "offline"; attempt?: number }) => void): void;
  openExternal?(url: string): Promise<{ opened: boolean }>;
  platform: string;
}

interface Window { zagent: ZAgentBridge; }
