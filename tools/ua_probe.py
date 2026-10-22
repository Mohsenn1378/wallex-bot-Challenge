"""Find request settings the CodeCraft gateway (Cloudflare) accepts:  python tools/ua_probe.py
Sends tiny 'say ok' requests to gpt-5.6-luna. Never prints the API key."""
import os, sys, json, subprocess, shutil, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallex_agent.llm.adapter import load_env
load_env()
key = os.environ.get("CODECRAFT_API_KEY", "")
print("key present:", bool(key), "length:", len(key))
URL = "https://codecraftapi.com/v1/chat/completions"
BODY = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "say ok"}]}
UAS = ["opencode/1.19.3", "opencode/2.0.32",
       "ai-sdk/openai-compatible/1.0.0 ai-sdk/provider-utils/3.0.0 runtime/node.js/22",
       "OpenAI/JS 4.77.0", "OpenAI/Python 1.54.0", "curl/8.4.0",
       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"]
node = shutil.which("node")
script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node_fetch.js")
curl = shutil.which("curl")
for ua in UAS:
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": ua}
    # python
    try:
        req = urllib.request.Request(URL, data=json.dumps(BODY).encode(), headers=hdr)
        with urllib.request.urlopen(req, timeout=30) as r:
            py = f"OK {r.status}"
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace"); py = f"{e.code} {'cloudflare' if 'Just a moment' in b else b[:80]}"
    except Exception as e:
        py = type(e).__name__
    # node
    nd = "-"
    if node:
        p = subprocess.run([node, script], input=json.dumps({"url": URL, "headers": hdr, "body": BODY, "timeout_ms": 30000}),
                           capture_output=True, text=True, encoding="utf-8")
        try:
            r = json.loads(p.stdout); t = r.get("text", "")
            nd = r.get("error") or f"{r['status']} {'cloudflare' if 'Just a moment' in t else t[:80]}"
        except Exception:
            nd = p.stdout[:80]
    print(f"UA={ua[:45]:45s} | python: {py[:60]:60s} | node: {nd[:80]}")
if curl:
    p = subprocess.run([curl, "-s", "-o", "-", "-w", "\\n%{http_code}", URL, "-H", f"Authorization: Bearer {key}",
                        "-H", "Content-Type: application/json", "-d", json.dumps(BODY)], capture_output=True, text=True)
    out = p.stdout.strip().splitlines()
    print("curl (system):", out[-1] if out else p.stderr[:80], "cloudflare" if "Just a moment" in p.stdout else p.stdout[:80])
