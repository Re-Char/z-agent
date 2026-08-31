function consumeSseFrames(buffer) {
  const events = [];
  let rest = buffer;
  while (true) {
    const boundary = rest.match(/\r?\n\r?\n/);
    if (!boundary || boundary.index === undefined) break;
    const raw = rest.slice(0, boundary.index);
    rest = rest.slice(boundary.index + boundary[0].length);
    const data = raw.split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n")
      .trim();
    if (!data) continue;
    try {
      events.push(JSON.parse(data));
    } catch (_) {
      // Ignore malformed frames; the Core will eventually emit a terminal error.
    }
  }
  return { events, rest };
}

module.exports = { consumeSseFrames };
