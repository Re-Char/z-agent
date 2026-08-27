interface ZAgentBridge {
  request<T>(path: string, options?: { method?: string; body?: unknown }): Promise<T>;
  platform: string;
}

interface Window { zagent: ZAgentBridge; }

