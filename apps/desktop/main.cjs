const { app, BrowserWindow, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const { existsSync } = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

let mainWindow;
let coreProcess;
let coreInfo;

function coreRoot() {
  return app.isPackaged ? path.join(process.resourcesPath, "core") : path.resolve(__dirname, "../..");
}

function startCore() {
  if (coreInfo) return Promise.resolve(coreInfo);
  return new Promise((resolve, reject) => {
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
    const timer = setTimeout(() => {
      if (!settled) reject(new Error("核心服务启动超时"));
    }, 15000);
    readline.createInterface({ input: coreProcess.stdout }).on("line", (line) => {
      try {
        const message = JSON.parse(line);
        if (message.ready) {
          settled = true;
          clearTimeout(timer);
          coreInfo = message;
          resolve(message);
        }
      } catch (_) {
        // Non-protocol stdout is ignored; diagnostics belong on stderr.
      }
    });
    coreProcess.stderr.on("data", (chunk) => console.error(`[core] ${chunk}`));
    coreProcess.once("error", (error) => {
      clearTimeout(timer);
      if (!settled) reject(error);
    });
    coreProcess.once("exit", (code) => {
      if (!settled) reject(new Error(`核心服务退出：${code}`));
      coreInfo = undefined;
    });
  });
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

ipcMain.handle("core:request", async (_event, request) => {
  if (!coreInfo) throw new Error("核心服务不可用");
  const method = request.method || "GET";
  const response = await fetch(`http://${coreInfo.host}:${coreInfo.port}${request.path}`, {
    method,
    headers: { "Authorization": `Bearer ${coreInfo.token}`, "Content-Type": "application/json" },
    body: method === "GET" ? undefined : JSON.stringify(request.body || {})
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败：${response.status}`);
  return payload;
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
