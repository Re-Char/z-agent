const assert = require("node:assert/strict");
const test = require("node:test");

const { MAX_JSON_EXPORT_BYTES, prepareJsonExport } = require("./export.cjs");

test("accepts valid JSON and normalizes the extension", () => {
  assert.deepEqual(prepareJsonExport({ suggestedName: "memory-backup", content: "{}\n" }), {
    content: "{}\n",
    safeName: "memory-backup.json"
  });
});

test("sanitizes renderer-controlled names and strips traversal prefixes", () => {
  const name = prepareJsonExport({ suggestedName: "../../记忆 backup.json", content: "[]" }).safeName;
  assert.equal(name.includes(".."), false);
  assert.equal(name.includes("/"), false);
  assert.match(name, /backup\.json$/);
});

test("rejects malformed JSON", () => {
  assert.throws(() => prepareJsonExport({ content: "{broken" }), /有效 JSON/);
});

test("rejects oversized exports", () => {
  assert.throws(
    () => prepareJsonExport({ content: `"${"x".repeat(MAX_JSON_EXPORT_BYTES)}"` }),
    /20 MiB/
  );
});
