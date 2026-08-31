const MAX_JSON_EXPORT_BYTES = 20 * 1024 * 1024;

function prepareJsonExport(request) {
  const content = request?.content;
  if (typeof content !== "string" || Buffer.byteLength(content, "utf8") > MAX_JSON_EXPORT_BYTES) {
    throw new Error("导出内容无效或超过 20 MiB 上限");
  }
  try {
    JSON.parse(content);
  } catch (_) {
    throw new Error("仅允许保存有效 JSON 导出");
  }
  const baseName = String(request?.suggestedName || "zagent-memories.json")
    .replace(/[^a-zA-Z0-9._-]/g, "-")
    .replace(/\.\.+/g, "-")
    .replace(/^\.+/, "")
    .slice(0, 120) || "zagent-memories.json";
  return {
    content,
    safeName: baseName.endsWith(".json") ? baseName : `${baseName}.json`
  };
}

module.exports = { MAX_JSON_EXPORT_BYTES, prepareJsonExport };
