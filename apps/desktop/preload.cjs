const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("zagent", {
  request: (path, options = {}) => ipcRenderer.invoke("core:request", {
    path,
    method: options.method || "GET",
    body: options.body
  }),
  // Streams a POST request: main process reads the SSE body and forwards events
  // to the renderer over a private IPC channel until done/error.
  requestStream: (path, options = {}, onEvent) => new Promise((resolve, reject) => {
    const channel = `core:stream:${Date.now()}:${Math.random().toString(36).slice(2)}`;
    const cleanup = () => ipcRenderer.removeAllListeners(channel);
    ipcRenderer.on(channel, (_event, payload) => {
      if (payload && (payload.type === "done" || payload.type === "error" || payload.type === "cancelled")) {
        cleanup();
        resolve(payload);
      } else if (payload && payload.type === "stream-error") {
        cleanup();
        reject(new Error(payload.message));
      } else if (onEvent) {
        onEvent(payload);
      }
    });
    ipcRenderer.invoke("core:stream", {
      path,
      method: options.method || "POST",
      body: options.body,
      channel
    }).catch((error) => {
      cleanup();
      reject(error);
    });
  }),
  cancelStream: () => ipcRenderer.invoke("core:stream-cancel"),
  // Native directory picker (Electron only); null when cancelled/unavailable.
  selectFolder: () => ipcRenderer.invoke("dialog:select-folder"),
  selectExtension: () => ipcRenderer.invoke("dialog:select-extension"),
  selectMcpConfig: () => ipcRenderer.invoke("dialog:select-mcp-config"),
  saveJson: (suggestedName, content) => ipcRenderer.invoke("dialog:save-json", { suggestedName, content }),
  oauthInfo: () => ipcRenderer.invoke("core:oauth-info"),
  onCoreStatus: (callback) => ipcRenderer.on("core:status", (_event, payload) => callback(payload)),
  openExternal: (url) => ipcRenderer.invoke("shell:open-external", url),
  platform: process.platform
});
