// Reads one JSON object per line, {"tex": string, "display": bool}, and writes
// one line per input: {"ok": true} or {"ok": false, "error": string}.
import katex from "katex";
import { createInterface } from "node:readline";

for await (const line of createInterface({ input: process.stdin })) {
  const { tex, display } = JSON.parse(line);
  try {
    katex.renderToString(tex, { displayMode: display, throwOnError: true, strict: "ignore" });
    console.log(JSON.stringify({ ok: true }));
  } catch (err) {
    console.log(JSON.stringify({ ok: false, error: String(err.message ?? err) }));
  }
}
