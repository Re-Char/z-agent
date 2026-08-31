const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { autoUpdater } = require("electron-updater");
const { spawn } = require("node:child_process");
const { existsSync, mkdirSync, writeFileSync } = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const { prepareJsonExport } = require("./export.cjs");
const { consumeSseFrames } = require("./sse.cjs");

let mainWindow;
let coreProcess;
let coreInfo;
let coreStartPromise;
let coreRestartTimer;
let coreRestartAttempts = 0;
let isQuitting = false;
const activeStreamControllers = new Map();

const DEFAULT_CORE_START_TIMEOUT_MS = 120000;
const MAX_CORE_RESTARTS = 3;

function coreStartTimeoutMs() {
  const configured = Number(process.env.ZAGENT_CORE_START_TIMEOUT_MS);
  return Number.isFinite(configured) && configured > 0
    ? configured
    : DEFAULT_CORE_START_TIMEOUT_MS;
}

function coreRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "core-runtime")
    : path.resolve(__dirname, "../..");
}

function coreCommand() {
  const root = coreRoot();
  if (app.isPackaged) {
    const executable = process.platform === "win32"
      ? path.join(root, "python.exe")
      : path.join(root, "bin", "python");
    if (!existsSync(executable)) {
      throw new Error(`内置 Core runtime 不完整：${executable}`);
    }
    return { root, executable };
  }
  const condaPython = process.platform === "win32"
    ? path.join(root, ".conda", "envs", "zagent", "python.exe")
    : path.join(root, ".conda", "envs", "zagent", "bin", "python");
  const executable = process.env.ZAGENT_PYTHON
    || (existsSync(condaPython) ? condaPython : (process.platform === "win32" ? "python" : "python3"));
  return { root, executable };
}

function recordCoreCrash(reason) {
  try {
    const diagnostics = path.join(app.getPath("userData"), "diagnostics");
    mkdirSync(diagnostics, { recursive: true, mode: 0o700 });
    writeFileSync(path.join(diagnostics, "last-core-crash.json"), JSON.stringify({
      timestamp: new Date().toISOString(),
      reason: String(reason).slice(0, 2000),
      restartAttempts: coreRestartAttempts
    }, null, 2), { mode: 0o600 });
  } catch (error) {
    console.error("failed to record core crash", error);
  }
}

function sendCoreStatus(payload) {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.webContents.send("core:status", payload);
  }
}

function scheduleCoreRestart(reason) {
  if (isQuitting || coreRestartTimer) return;
  if (coreRestartAttempts >= MAX_CORE_RESTARTS) {
    sendCoreStatus({ status: "offline", reason: "核心服务连续恢复失败" });
    return;
  }
  recordCoreCrash(reason);
  const delay = 1000 * (2 ** coreRestartAttempts);
  coreRestartAttempts += 1;
  sendCoreStatus({ status: "recovering", attempt: coreRestartAttempts, delay });
  coreRestartTimer = setTimeout(async () => {
    coreRestartTimer = undefined;
    try {
      await startCore();
      await waitForCore();
      coreRestartAttempts = 0;
      sendCoreStatus({ status: "online", recovered: true });
    } catch (error) {
      scheduleCoreRestart(error);
    }
  }, delay);
}

function startCore() {
  if (coreInfo) return Promise.resolve(coreInfo);
  if (coreStartPromise) return coreStartPromise;
  coreStartPromise = new Promise((resolve, reject) => {
    let command;
    try {
      command = coreCommand();
    } catch (error) {
      coreStartPromise = undefined;
      reject(error);
      return;
    }
    const { root, executable } = command;
    const args = ["-m", "zagent.server", "--port", "0", "--data-dir", path.join(app.getPath("userData"), "core"),
      "--project-dir", process.cwd()];
    const childEnvironment = {
      ...process.env,
      PYTHONPYCACHEPREFIX: path.join(app.getPath("temp"), "zagent-pycache")
    };
    if (!app.isPackaged) childEnvironment.PYTHONPATH = path.join(root, "src");
    coreProcess = spawn(executable, args, {
      cwd: root,
      env: childEnvironment,
      stdio: ["ignore", "pipe", "pipe"]
    });
    let settled = false;
    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      coreStartPromise = undefined;
      if (coreProcess && !coreProcess.killed) coreProcess.kill("SIGTERM");
      reject(error);
    };
    const timer = setTimeout(() => {
      fail(new Error(`核心服务启动超时（${Math.round(coreStartTimeoutMs() / 1000)} 秒）`));
    }, coreStartTimeoutMs());
    readline.createInterface({ input: coreProcess.stdout }).on("line", (line) => {
      try {
        const message = JSON.parse(line);
        if (message.ready) {
          settled = true;
          clearTimeout(timer);
          coreInfo = message;
          coreStartPromise = undefined;
          resolve(message);
        }
      } catch (_) {
        // Non-protocol stdout is ignored; diagnostics belong on stderr.
      }
    });
    coreProcess.stderr.on("data", (chunk) => console.error(`[core] ${chunk}`));
    coreProcess.once("error", (error) => {
      fail(error);
    });
    coreProcess.once("exit", (code) => {
      const wasReady = settled;
      if (!settled) fail(new Error(`核心服务退出：${code}`));
      coreInfo = undefined;
      coreProcess = undefined;
      if (wasReady && !isQuitting) scheduleCoreRestart(`核心服务异常退出：${code}`);
    });
  });
  return coreStartPromise;
}

async function ensureCore() {
  if (!coreInfo) await startCore();
  await waitForCore();
}

async function waitForCore() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch(`http://${coreInfo.host}:${coreInfo.port}/health`);
      if (response.ok) return;
    } catch (_) {
      // The process announced its selected port before uvicorn completed binding.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("核心服务未能进入就绪状态");
}

async function createWindow() {
  await ensureCore();
  mainWindow = new BrowserWindow({
    width: 1380,
    height: 900,
    minWidth: 980,
    minHeight: 680,
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    backgroundColor: "#0b0e14",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  if (process.env.ZAGENT_DEV_SERVER_URL) {
    await mainWindow.loadURL(process.env.ZAGENT_DEV_SERVER_URL);
  } else {
    await mainWindow.loadFile(path.join(__dirname, "ui", "index.html"));
  }
}

function setupAutoUpdates() {
  if (!app.isPackaged) return;
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.logger = console;
  autoUpdater.on("error", (error) => console.error("auto update failed", error));
  autoUpdater.on("update-downloaded", async (info) => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    const choice = await dialog.showMessageBox(mainWindow, {
      type: "info",
      buttons: ["重启并安装", "稍后"],
      defaultId: 0,
      cancelId: 1,
      title: "Z-Agent 更新已就绪",
      message: `版本 ${info.version} 已下载完成。`,
      detail: "重启后将自动安装；选择稍后会在退出应用时安装。"
    });
    if (choice.response === 0) autoUpdater.quitAndInstall(false, true);
  });
  const check = () => autoUpdater.checkForUpdates().catch((error) => {
    console.error("update check failed", error);
  });
  setTimeout(check, 10000);
  setInterval(check, 6 * 60 * 60 * 1000);
}

function responseError(payload, statusCode) {
  if (typeof payload?.error === "string") return payload.error;
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail)) {
    const messages = payload.detail.map((item) => item?.msg).filter(Boolean);
    if (messages.length) return messages.join("；");
  }
  return `请求失败：${statusCode}`;
}

ipcMain.handle("core:request", async (_event, request) => {
  await ensureCore();
  const method = request.method || "GET";
  const response = await fetch(`http://${coreInfo.host}:${coreInfo.port}${request.path}`, {
    method,
    headers: { "Authorization": `Bearer ${coreInfo.token}`, "Content-Type": "application/json" },
    body: method === "GET" ? undefined : JSON.stringify(request.body || {})
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(responseError(payload, response.status));
  return payload;
});

ipcMain.handle("core:oauth-info", async () => {
  await ensureCore();
  return {
    redirectUri: `http://${coreInfo.host}:${coreInfo.port}/v1/mcp/oauth/callback/browser`
  };
});

ipcMain.handle("shell:open-external", async (_event, url) => {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(parsed.hostname))) {
    throw new Error("仅允许打开 HTTPS 或本机 HTTP URL");
  }
  await shell.openExternal(parsed.toString());
  return { opened: true };
});

ipcMain.handle("core:stream", async (event, request) => {
  await ensureCore();
  const senderId = event.sender.id;
  activeStreamControllers.get(senderId)?.controller.abort();
  const controller = new AbortController();
  activeStreamControllers.set(senderId, { controller, channel: request.channel, sender: event.sender });
  try {
    const response = await fetch(`http://${coreInfo.host}:${coreInfo.port}${request.path}`, {
      method: request.method || "POST",
      headers: { "Authorization": `Bearer ${coreInfo.token}`, "Content-Type": "application/json" },
      body: JSON.stringify(request.body || {}),
      signal: controller.signal
    });
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(responseError(payload, response.status));
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = consumeSseFrames(buffer);
      buffer = parsed.rest;
      for (const payload of parsed.events) {
        // Provider reasoning is required internally for some tool-call protocols,
        // but it is never part of the renderer contract or product UI.
        if (payload.type === "reasoning") continue;
        event.sender.send(request.channel, payload);
        if (payload.type === "done" || payload.type === "error") return;
      }
    }
    if (!controller.signal.aborted) {
      event.sender.send(request.channel, { type: "stream-error", message: "模型数据流意外结束" });
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      event.sender.send(request.channel, { type: "stream-error", message: String(error) });
    }
  } finally {
    if (activeStreamControllers.get(senderId)?.controller === controller) {
      activeStreamControllers.delete(senderId);
    }
  }
});

ipcMain.handle("core:stream-cancel", (event) => {
  const active = activeStreamControllers.get(event.sender.id);
  if (!active) return { cancelled: false };
  active.sender.send(active.channel, { type: "cancelled" });
  active.controller.abort();
  activeStreamControllers.delete(event.sender.id);
  return { cancelled: true };
});

ipcMain.handle("dialog:select-folder", async () => {
  if (!mainWindow) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "选择工作区目录（agent 的安全边界）",
    properties: ["openDirectory", "createDirectory"]
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

ipcMain.handle("dialog:select-extension", async () => {
  if (!mainWindow) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "选择 Z-Agent 扩展目录或 ZIP 包",
    properties: ["openFile", "openDirectory"],
    filters: [
      { name: "Z-Agent Extension", extensions: ["zip"] },
      { name: "All Files", extensions: ["*"] }
    ]
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

ipcMain.handle("dialog:select-mcp-config", async () => {
  if (!mainWindow) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    title: "选择 MCP 配置或 MCP Bundle",
    properties: ["openFile"],
    filters: [
      { name: "MCP Config / Bundle", extensions: ["json", "mcpb", "dxt"] },
      { name: "All Files", extensions: ["*"] }
    ]
  });
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

ipcMain.handle("dialog:save-json", async (_event, request) => {
  if (!mainWindow) return null;
  const { content, safeName } = prepareJsonExport(request);
  const result = await dialog.showSaveDialog(mainWindow, {
    title: "导出 Z-Agent 长期记忆",
    defaultPath: safeName,
    filters: [{ name: "JSON", extensions: ["json"] }]
  });
  if (result.canceled || !result.filePath) return null;
  writeFileSync(result.filePath, content, { encoding: "utf8", mode: 0o600 });
  return result.filePath;
});

app.whenReady().then(async () => {
  await createWindow();
  setupAutoUpdates();
}).catch((error) => {
  recordCoreCrash(error);
  console.error(error);
  app.quit();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on("before-quit", () => {
  isQuitting = true;
  if (coreRestartTimer) clearTimeout(coreRestartTimer);
  if (coreProcess && !coreProcess.killed) coreProcess.kill("SIGTERM");
});
