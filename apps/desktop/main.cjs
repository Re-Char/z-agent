const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

let mainWindow;
let coreProcess;
let coreInfo;
let coreStartPromise;
const activeStreamControllers = new Map();

const DEFAULT_CORE_START_TIMEOUT_MS = 120000;

function coreStartTimeoutMs() {
  const configured = Number(process.env.ZAGENT_CORE_START_TIMEOUT_MS);
  return Number.isFinite(configured) && configured > 0
    ? configured
    : DEFAULT_CORE_START_TIMEOUT_MS;
}

function coreRoot() {
  return app.isPackaged ? path.join(process.resourcesPath, "core") : path.resolve(__dirname, "../..");
}

function startCore() {
  if (coreInfo) return Promise.resolve(coreInfo);
  if (coreStartPromise) return coreStartPromise;
  coreStartPromise = new Promise((resolve, reject) => {
    const root = coreRoot();
    const condaPython = process.platform === "win32"
      ? path.join(root, ".conda", "envs", "zagent", "python.exe")
      : path.join(root, ".conda", "envs", "zagent", "bin", "python");
    const python = process.env.ZAGENT_PYTHON || (existsSync(condaPython) ? condaPython : (process.platform === "win32" ? "python" : "python3"));
    const args = ["-m", "zagent.server", "--port", "0", "--data-dir", path.join(app.getPath("userData"), "core"),
      "--project-dir", process.cwd()];
    coreProcess = spawn(python, args, {
      cwd: root,
      env: {
        ...process.env,
        PYTHONPATH: path.join(root, "src"),
        PYTHONPYCACHEPREFIX: path.join(app.getPath("temp"), "zagent-pycache")
      },
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
      if (!settled) fail(new Error(`核心服务退出：${code}`));
      coreInfo = undefined;
      coreProcess = undefined;
    });
  });
  return coreStartPromise;
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
  await startCore();
  await waitForCore();
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
  if (!coreInfo) throw new Error("核心服务不可用");
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

ipcMain.handle("core:stream", async (event, request) => {
  if (!coreInfo) throw new Error("核心服务不可用");
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
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, boundary).trim();
        buffer = buffer.slice(boundary + 2);
        if (!rawEvent.startsWith("data:")) continue;
        const data = rawEvent.slice(5).trim();
        if (!data) continue;
        let payload;
        try {
          payload = JSON.parse(data);
        } catch (_) {
          continue;
        }
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

app.whenReady().then(createWindow).catch((error) => {
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
  if (coreProcess && !coreProcess.killed) coreProcess.kill("SIGTERM");
});
