const test = require("node:test");
const assert = require("node:assert/strict");

const { consumeSseFrames } = require("./sse.cjs");

test("parses LF and CRLF progress frames", () => {
  const input = 'data: {"type":"status","round":1}\n\n'
    + 'data: {"type":"tool_call","name":"fs_write"}\r\n\r\n';
  const parsed = consumeSseFrames(input);
  assert.deepEqual(parsed.events, [
    { type: "status", round: 1 },
    { type: "tool_call", name: "fs_write" },
  ]);
  assert.equal(parsed.rest, "");
});

test("keeps a split frame until the next chunk", () => {
  const first = consumeSseFrames('data: {"type":"tool_result"');
  assert.deepEqual(first.events, []);
  const second = consumeSseFrames(first.rest + ',"name":"fs_write","ok":true}\n\n');
  assert.deepEqual(second.events, [{ type: "tool_result", name: "fs_write", ok: true }]);
  assert.equal(second.rest, "");
});

test("forwards permission request frames without losing structured details", () => {
  const parsed = consumeSseFrames(
    'data: {"type":"permission_required","request":{"request_id":"prm_1","details":{"network":false}}}\r\n\r\n'
  );
  assert.deepEqual(parsed.events, [{
    type: "permission_required",
    request: { request_id: "prm_1", details: { network: false } },
  }]);
});
