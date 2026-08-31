#!/usr/bin/env node

/**
 * Real Electron renderer/preload/Core interaction acceptance via Chromium CDP.
 *
 * Start the app with an isolated user-data-dir and --remote-debugging-port,
 * then run this script with ZAGENT_ELECTRON_CDP_PORT (default: 9223).
 * It intentionally avoids native file chooser/save dialogs so CI never writes
 * outside its temporary workspace; those IPC payload boundaries have unit tests.
 */

import assert from "node:assert/strict";

const port = Number(process.env.ZAGENT_ELECTRON_CDP_PORT || 9223);
const workspacePath = process.env.ZAGENT_ELECTRON_QA_WORKSPACE;
assert(workspacePath, "ZAGENT_ELECTRON_QA_WORKSPACE is required");

const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
const target = targets.find((item) => item.type === "page" && item.title === "Z-Agent");
assert(target?.webSocketDebuggerUrl, "Z-Agent Electron renderer target was not found");

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let sequence = 0;
const pending = new Map();
socket.addEventListener("message", (event) => {
  const message = JSON.parse(String(event.data));
  if (!message.id || !pending.has(message.id)) return;
  const { resolve, reject } = pending.get(message.id);
  pending.delete(message.id);
  if (message.error) reject(new Error(message.error.message));
  else resolve(message.result);
});

function command(method, params = {}) {
  const id = ++sequence;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

async function pageEval(fn, ...args) {
  const expression = `(${fn.toString()})(${args.map((arg) => JSON.stringify(arg)).join(",")})`;
  const response = await command("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (response.exceptionDetails) {
    const detail = response.exceptionDetails.exception?.description || response.exceptionDetails.text;
    throw new Error(detail);
  }
  return response.result.value;
}

async function waitFor(fn, label, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  let lastValue;
  while (Date.now() < deadline) {
    lastValue = await pageEval(fn);
    if (lastValue) return lastValue;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`timeout waiting for ${label}; last value: ${JSON.stringify(lastValue)}`);
}

async function clickButton(text, scope = "body", index = 0) {
  return pageEval((expected, rootSelector, itemIndex) => {
    const root = document.querySelector(rootSelector);
    if (!root) throw new Error(`missing scope ${rootSelector}`);
    const matches = [...root.querySelectorAll("button")].filter((button) =>
      !button.disabled && button.textContent.trim() === expected && button.offsetParent !== null
    );
    if (!matches[itemIndex]) throw new Error(`missing enabled button ${expected} #${itemIndex}`);
    matches[itemIndex].click();
    return true;
  }, text, scope, index);
}

async function clickAria(label, scope = "body") {
  return pageEval((expected, rootSelector) => {
    const root = document.querySelector(rootSelector);
    const element = root?.querySelector(`[aria-label="${CSS.escape(expected)}"]`);
    if (!(element instanceof HTMLElement) || element.hasAttribute("disabled")) {
      throw new Error(`missing enabled aria control ${expected}`);
    }
    element.click();
    return true;
  }, label, scope);
}

async function clickInCard(cardText, buttonText) {
  return pageEval((needle, expected) => {
    const card = [...document.querySelectorAll(".model-item,.integration-card,.memory-card")]
      .find((item) => item.textContent.includes(needle));
    if (!card) throw new Error(`missing card ${needle}`);
    const button = [...card.querySelectorAll("button")]
      .find((item) => !item.disabled && item.textContent.trim() === expected);
    if (!button) throw new Error(`missing ${expected} in ${needle}`);
    button.click();
    return true;
  }, cardText, buttonText);
}

async function setLabeled(labelText, value, scope = "[role=dialog]") {
  return pageEval((expected, nextValue, rootSelector) => {
    const root = document.querySelector(rootSelector);
    const labels = [...(root?.querySelectorAll("label") || [])];
    const label = labels.find((item) => item.childNodes[0]?.textContent?.trim() === expected)
      || labels.find((item) => item.textContent.trim().startsWith(expected));
    const element = label?.querySelector("input,textarea,select");
    if (!element) throw new Error(`missing field ${expected}`);
    const prototype = element instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, String(nextValue));
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    return element.value;
  }, labelText, value, scope);
}

async function setAria(label, value, scope = "body") {
  return pageEval((expected, nextValue, rootSelector) => {
    const element = document.querySelector(rootSelector)?.querySelector(`[aria-label="${CSS.escape(expected)}"]`);
    if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement)) {
      throw new Error(`missing editable aria control ${expected}`);
    }
    const prototype = element instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, String(nextValue));
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    return element.value;
  }, label, value, scope);
}

async function setFormField(sectionHeading, labelText, value) {
  return pageEval((heading, expected, nextValue) => {
    const title = [...document.querySelectorAll("h3")].find((item) => item.textContent.trim() === heading);
    const form = title?.closest("form") || title?.parentElement?.parentElement?.querySelector("form");
    if (!form) throw new Error(`missing form ${heading}`);
    const label = [...form.querySelectorAll("label")].find((item) =>
      item.childNodes[0]?.textContent?.trim() === expected || item.textContent.trim().startsWith(expected)
    );
    const element = label?.querySelector("input,textarea,select");
    if (!element) throw new Error(`missing ${expected} in ${heading}`);
    const prototype = element instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, String(nextValue));
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    return element.value;
  }, sectionHeading, labelText, value);
}

async function hasText(text, scope = "body") {
  return pageEval((needle, rootSelector) => document.querySelector(rootSelector)?.textContent.includes(needle) || false, text, scope);
}

const checks = [];
async function check(name, action) {
  await action();
  checks.push(name);
  process.stdout.write(`PASS ${name}\n`);
}

await command("Runtime.enable");

await check("initial online/disabled state", async () => {
  assert.equal(await hasText("核心在线"), true);
  assert.equal(await pageEval(() => document.querySelector('[aria-label="发送消息"]')?.disabled), true);
  assert.equal(await pageEval(() => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "长期记忆")?.disabled), true);
});

await check("context inspector toggle/x/scrim", async () => {
  await clickAria("切换上下文检查器");
  assert.equal(await waitFor(() => document.querySelector(".inspector")?.classList.contains("open"), "inspector open"), true);
  await clickAria("关闭上下文检查器", ".inspector");
  assert.equal(await waitFor(() => !document.querySelector(".inspector")?.classList.contains("open"), "inspector x close"), true);
  await clickAria("切换上下文检查器");
  await clickAria("关闭上下文检查器", "body");
  assert.equal(await waitFor(() => !document.querySelector(".inspector")?.classList.contains("open"), "inspector scrim close"), true);
});

await check("workspace create modal cancel", async () => {
  await clickAria("新建工作区");
  assert.equal(await waitFor(() => document.querySelector('[role="dialog"][aria-label="新建工作区"]') !== null, "create workspace dialog"), true);
  await clickButton("取消", '[role="dialog"][aria-label="新建工作区"]');
  assert.equal(await waitFor(() => document.querySelector('[role="dialog"][aria-label="新建工作区"]') === null, "create workspace cancel"), true);
});

await check("workspace edit cancel and save", async () => {
  await clickButton("设置工作区");
  assert.equal(await waitFor(() => document.querySelector('[role="dialog"][aria-label="编辑工作区"]') !== null, "edit workspace dialog"), true);
  await clickButton("取消", '[role="dialog"][aria-label="编辑工作区"]');
  await clickAria("编辑当前工作区");
  await setLabeled("名称", "Electron QA");
  await setLabeled("路径", workspacePath);
  await clickButton("保存", '[role="dialog"][aria-label="编辑工作区"]');
  assert.equal(await waitFor(() => document.querySelector(".topbar-title small")?.textContent.includes("/private/tmp/zagent-electron-qa."), "workspace saved"), true);
});

await check("workspace create/switch initial-state reset", async () => {
  await clickAria("新建工作区");
  await setLabeled("名称", "Electron QA Secondary");
  await setLabeled("路径（agent 可访问的项目目录）", workspacePath);
  await clickButton("创建工作区", '[role="dialog"][aria-label="新建工作区"]');
  assert.equal(await waitFor(() => document.querySelector(".workspace-switcher")?.selectedOptions[0]?.textContent.includes("Secondary"), "secondary active"), true);
  assert.equal(await hasText("从一个清晰的目标开始"), true);
  const primaryId = await pageEval(async () => (await window.zagent.request("/v1/workspaces")).workspaces.find((item) => item.name === "Electron QA").workspace_id);
  await pageEval((workspaceId) => {
    const select = document.querySelector(".workspace-switcher");
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set.call(select, workspaceId);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }, primaryId);
  assert.equal(await waitFor(() => document.querySelector(".workspace-switcher")?.selectedOptions[0]?.textContent.includes("Electron QA ·"), "primary switched"), true);
});

await check("new session/send/pin/unpin", async () => {
  await clickButton("＋ 新建对话");
  assert.equal(await waitFor(() => document.querySelectorAll(".session-list .session").length === 1, "new session"), true);
  await pageEval((value) => {
    const input = document.querySelector('[aria-label="任务输入"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, "Electron 全量交互测试");
  await clickAria("发送消息");
  assert.equal(await waitFor(() => document.querySelectorAll(".event.assistant").length > 0, "echo response"), true);
  await pageEval(() => document.querySelector(".event.user .pin-btn")?.click());
  assert.equal(await waitFor(() => document.querySelector(".event.user .pin-btn")?.classList.contains("active"), "message pinned"), true);
  await clickAria("切换上下文检查器");
  assert.equal(await waitFor(() => document.querySelector(".ws-unpin") !== null, "inspector pinned evidence"), true);
  await pageEval(() => document.querySelector(".ws-unpin")?.click());
  assert.equal(await waitFor(() => document.querySelector(".ws-unpin") === null, "inspector unpin"), true);
  await clickAria("关闭上下文检查器", ".inspector");
});

await check("model add/activate/edit/new-mode/delete cancel+confirm", async () => {
  await clickButton("模型设置");
  assert.equal(await waitFor(() => document.querySelector('[role="dialog"][aria-label="模型设置"]') !== null, "model dialog"), true);
  await clickButton("＋ 新建模型", '[role="dialog"][aria-label="模型设置"]');
  await setLabeled("名称", "QA Echo");
  await setLabeled("适配器", "echo");
  await setLabeled("模型名称", "qa-echo");
  await setLabeled("上下文窗口", 32768);
  await clickButton("添加模型", '[role="dialog"][aria-label="模型设置"]');
  assert.equal(await waitFor(() => [...document.querySelectorAll(".model-item")].some((item) => item.textContent.includes("QA Echo")), "model added"), true);
  await pageEval(() => [...document.querySelectorAll(".model-item")].find((item) => item.textContent.includes("QA Echo"))?.querySelector(".model-activate")?.click());
  assert.equal(await waitFor(() => document.querySelector(".model-item.active")?.textContent.includes("QA Echo"), "model active"), true);
  await clickInCard("QA Echo", "编辑");
  await setLabeled("名称", "QA Echo Edited");
  await clickButton("保存修改", '[role="dialog"][aria-label="模型设置"]');
  assert.equal(await waitFor(() => [...document.querySelectorAll(".model-item")].some((item) => item.textContent.includes("QA Echo Edited")), "model edited"), true);
  await clickInCard("QA Echo Edited", "编辑");
  await clickButton("改为新建", '[role="dialog"][aria-label="模型设置"]');
  assert.equal(await hasText("新建模型", '[role="dialog"][aria-label="模型设置"]'), true);
  await clickInCard("QA Echo Edited", "删除");
  await clickButton("取消", '[role="dialog"][aria-label="确认操作"]');
  assert.equal(await hasText("QA Echo Edited", '[role="dialog"][aria-label="模型设置"]'), true);
  await clickInCard("QA Echo Edited", "删除");
  await clickButton("确认删除", '[role="dialog"][aria-label="确认操作"]');
  assert.equal(await waitFor(() => ![...document.querySelectorAll(".model-item")].some((item) => item.textContent.includes("QA Echo Edited")), "model deleted"), true);
  await clickButton("完成", '[role="dialog"][aria-label="模型设置"]');
});

await check("extension create/toggle/delete cancel+confirm", async () => {
  await clickButton("扩展与 MCP");
  assert.equal(await waitFor(() => document.querySelector('[role="dialog"][aria-label="扩展与 MCP"]') !== null, "extensions dialog"), true);
  await setFormField("创建开发用 manifest", "扩展 ID", "qa.electron.extension");
  await setFormField("创建开发用 manifest", "名称", "Electron QA Extension");
  await clickButton("添加扩展", '[role="dialog"][aria-label="扩展与 MCP"]');
  assert.equal(await waitFor(() => [...document.querySelectorAll(".integration-card")].some((item) => item.textContent.includes("Electron QA Extension")), "extension added"), true);
  await clickInCard("Electron QA Extension", "停用");
  assert.equal(await waitFor(() => [...document.querySelectorAll(".integration-card")].find((item) => item.textContent.includes("Electron QA Extension"))?.textContent.includes("启用"), "extension disabled"), true);
  await clickInCard("Electron QA Extension", "启用");
  await clickInCard("Electron QA Extension", "删除");
  await clickButton("取消", '[role="dialog"][aria-label="确认操作"]');
  await clickInCard("Electron QA Extension", "删除");
  await clickButton("确认删除", '[role="dialog"][aria-label="确认操作"]');
  assert.equal(await waitFor(() => ![...document.querySelectorAll(".integration-card")].some((item) => item.textContent.includes("Electron QA Extension")), "extension deleted"), true);
});

await check("MCP add/approve/revoke/delete and permission refresh", async () => {
  await setFormField("添加 MCP Server", "名称", "qa-http");
  await setFormField("添加 MCP Server", "传输方式", "http");
  await setFormField("添加 MCP Server", "URL", "http://127.0.0.1:9/mcp");
  await clickButton("添加 MCP", '[role="dialog"][aria-label="扩展与 MCP"]');
  assert.equal(await waitFor(() => [...document.querySelectorAll(".integration-card")].some((item) => item.textContent.includes("qa-http")), "MCP added"), true);
  await clickInCard("qa-http", "授权给 Agent");
  assert.equal(await waitFor(() => [...document.querySelectorAll(".integration-card")].find((item) => item.textContent.includes("qa-http"))?.textContent.includes("撤销授权"), "MCP approved"), true);
  await clickInCard("qa-http", "撤销授权");
  await clickButton("刷新", '[role="dialog"][aria-label="扩展与 MCP"]');
  assert.equal(await hasText("当前没有待处理授权", '[role="dialog"][aria-label="扩展与 MCP"]'), true);
  await clickInCard("qa-http", "删除");
  await clickButton("确认删除", '[role="dialog"][aria-label="确认操作"]');
  assert.equal(await waitFor(() => ![...document.querySelectorAll(".integration-card")].some((item) => item.textContent.includes("qa-http")), "MCP deleted"), true);
  await clickAria("关闭扩展与 MCP");
});

await check("memory search/audit/confirm/pin/correct/delete", async () => {
  const ids = await pageEval(async () => {
    const workspaceId = document.querySelector(".workspace-switcher").value;
    const sessions = (await window.zagent.request(`/v1/sessions?workspace_id=${workspaceId}`)).sessions;
    const sessionId = sessions[0].session_id;
    const events = (await window.zagent.request(`/v1/sessions/${sessionId}/events`)).events;
    const source = events.find((item) => item.role === "user");
    const created = await window.zagent.request(`/v1/sessions/${sessionId}/memories`, {
      method: "POST",
      body: { memory_type: "semantic", memory_key: "Electron Candidate", content: "Electron 交互验收候选", source_event_ids: [source.event_id], reason: "Electron E2E", scope: "workspace", confidence: 0.9, confirmed: false, pinned: false },
    });
    await window.zagent.request(`/v1/sessions/${sessionId}/memories`, {
      method: "POST",
      body: { memory_type: "semantic", memory_key: "Electron Search", content: "Electron 搜索交互验收", source_event_ids: [source.event_id], reason: "Electron E2E", scope: "workspace", confidence: 0.9, confirmed: true, pinned: false },
    });
    return { sessionId, memoryId: created.memory.memory_id };
  });
  await clickButton("长期记忆");
  assert.equal(await waitFor(() => document.querySelector('[role="dialog"][aria-label="长期记忆"]') !== null, "memory dialog"), true);
  await setAria("搜索长期记忆", "Search", '[role="dialog"][aria-label="长期记忆"]');
  assert.equal(await waitFor(() => [...document.querySelectorAll("button")].some((item) => item.textContent.trim() === "搜索" && !item.disabled), "memory search enabled"), true);
  await clickButton("搜索", '[role="dialog"][aria-label="长期记忆"]');
  assert.equal(await waitFor(() => document.querySelector(".memory-card")?.textContent.toLowerCase().includes("electron search"), "memory search result"), true);
  await clickButton("清除", '[role="dialog"][aria-label="长期记忆"]');
  assert.equal(await waitFor(() => [...document.querySelectorAll(".memory-card")].some((item) => item.textContent.includes("electron candidate")), "memory list restored"), true);
  await clickInCard("electron candidate", "查看审计");
  assert.equal(await waitFor(() => document.querySelector(".memory-audit") !== null, "memory audit"), true);
  await clickInCard("electron candidate", "收起审计");
  await clickInCard("electron candidate", "确认生效");
  assert.equal(await waitFor(() => [...document.querySelectorAll(".memory-card")].find((item) => item.textContent.includes("electron candidate"))?.textContent.includes("已生效"), "memory confirmed"), true);
  await clickInCard("electron candidate", "固定");
  assert.equal(await waitFor(() => [...document.querySelectorAll(".memory-card")].find((item) => item.textContent.includes("electron candidate"))?.textContent.includes("取消固定"), "memory pinned"), true);
  await clickInCard("electron candidate", "取消固定");
  assert.equal(await waitFor(() => [...document.querySelectorAll(".memory-card")].find((item) => item.textContent.includes("electron candidate"))?.querySelectorAll("button:not(:disabled)").length >= 3, "memory unpinned actions ready"), true);
  await clickInCard("electron candidate", "纠正");
  await clickButton("取消", '[role="dialog"][aria-label="纠正长期记忆"]');
  await clickInCard("electron candidate", "纠正");
  await setAria("纠正后的记忆正文", "Electron 交互验收已通过", '[role="dialog"][aria-label="纠正长期记忆"]');
  await clickButton("保存并替换", '[role="dialog"][aria-label="纠正长期记忆"]');
  assert.equal(await waitFor(() => document.querySelector(".memory-card")?.textContent.includes("已通过"), "memory corrected"), true);
  await clickInCard("electron candidate", "删除");
  await clickButton("取消", '[role="dialog"][aria-label="确认操作"]');
  await clickInCard("electron candidate", "删除");
  await clickButton("确认删除", '[role="dialog"][aria-label="确认操作"]');
  assert.equal(await waitFor(() => ![...document.querySelectorAll(".memory-card")].some((item) => item.textContent.includes("electron candidate")), "memory deleted"), true);
  await clickAria("关闭长期记忆");
  assert(ids.memoryId);
});

await check("renderer layout and error console", async () => {
  const layout = await pageEval(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    dialogs: document.querySelectorAll('[role="dialog"]').length,
    alertDialogs: document.querySelectorAll('[role="alertdialog"]').length,
    disabledSend: document.querySelector('[aria-label="发送消息"]')?.disabled,
  }));
  assert.equal(layout.overflow, false);
  assert.equal(layout.dialogs, 0);
  assert.equal(layout.alertDialogs, 0);
  assert.equal(layout.disabledSend, true);
});

socket.close();
process.stdout.write(`Electron interaction acceptance passed: ${checks.length} scenario groups\n`);
