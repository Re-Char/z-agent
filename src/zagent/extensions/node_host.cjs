"use strict";

const readline = require("node:readline");
const path = require("node:path");

const args = process.argv.slice(2);
const root = path.resolve(args[args.indexOf("--root") + 1] || "");
const entry = path.resolve(root, args[args.indexOf("--entry") + 1] || "");
if (!entry.startsWith(`${root}${path.sep}`)) throw new Error("invalid Node extension entry");
const extension = require(entry);
if (!Array.isArray(extension.tools) || typeof extension.invoke !== "function") {
  throw new Error("extension must export tools and invoke(name, arguments)");
}

function respond(id, result, error) {
  const payload = error
    ? { jsonrpc: "2.0", id, error: { code: -32000, message: String(error) } }
    : { jsonrpc: "2.0", id, result };
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

readline.createInterface({ input: process.stdin }).on("line", async (line) => {
  const message = JSON.parse(line);
  const method = message.method;
  if (method === "notifications/initialized" || method === "notifications/cancelled" || message.id === undefined) return;
  try {
    let result;
    if (method === "initialize") {
      result = {
        protocolVersion: "2025-11-25",
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "zagent-node-extension-host", version: "0.2.2", pid: process.pid },
      };
    } else if (method === "tools/list") {
      result = { tools: extension.tools };
    } else if (method === "tools/call") {
      const value = await extension.invoke(message.params?.name || "", message.params?.arguments || {});
      result = value && Array.isArray(value.content) ? value : {
        content: [{ type: "text", text: JSON.stringify(value) }],
        structuredContent: value && typeof value === "object" ? value : { value },
        isError: false,
      };
    } else {
      throw new Error(`unsupported method: ${method}`);
    }
    respond(message.id, result);
  } catch (error) {
    respond(message.id, undefined, error);
  }
});
