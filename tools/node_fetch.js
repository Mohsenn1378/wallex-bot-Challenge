// Tiny HTTP client used by the LLM adapter when Python requests are blocked (e.g. Cloudflare bot check).
// stdin: {"url","method","headers","body","timeout_ms"}  ->  stdout: {"status": int, "text": str} or {"error": str}
let input = "";
process.stdin.on("data", d => (input += d));
process.stdin.on("end", async () => {
  try {
    const req = JSON.parse(input);
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), req.timeout_ms || 60000);
    const r = await fetch(req.url, {
      method: req.method || "POST",
      headers: req.headers || {},
      body: req.body ? JSON.stringify(req.body) : undefined,
      signal: ctrl.signal,
    });
    clearTimeout(t);
    process.stdout.write(JSON.stringify({ status: r.status, text: await r.text() }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: String(e) }));
  }
});
