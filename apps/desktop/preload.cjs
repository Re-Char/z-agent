const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("zagent", {
  request: (path, options = {}) => ipcRenderer.invoke("core:request", {
    path,
    method: options.method || "GET",
    body: options.body
  }),
  platform: process.platform
});

