#!/usr/bin/env python3
"""Behavioural check — hello-engine reads demo.ping through the broker under its run token (commit cb33847).

Runs the UNMODIFIED engine as the launcher would (subprocess, RUN_TOKEN + AIRAWAT_API) against a local fake broker:
  • it takes its tenant from GET /v1/context (never hard-coded) and counts demo.ping via POST /v1/query;
  • every request carries the run token (X-Run-Token);
  • it prints RESULT {"tenant", "demo_ping_read"} with the right count;
  • with no tenant in the broker context it FAILS loudly (non-zero exit).
Exit 1 on failure.
"""
import http.server, json, os, subprocess, sys, threading

HERE = os.path.dirname(os.path.abspath(__file__))
fails = []
STATE = {"tenant": "testcity", "rows": 3, "tokens": set()}


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + detail if detail else ""))
    if not ok:
        fails.append(name)


class Broker(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        STATE["tokens"].add(self.headers.get("X-Run-Token"))
        self._send({"tenant": STATE["tenant"]} if self.path == "/v1/context" else {})

    def do_POST(self):
        STATE["tokens"].add(self.headers.get("X-Run-Token"))
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        ok = self.path == "/v1/query" and (body.get("filter") or {}).get("schema") == "demo.ping"
        self._send({"results": [{"object": {"schema": "demo.ping", "id": "p%d" % i}} for i in range(STATE["rows"])] if ok else []})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Broker)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "AIRAWAT_API": "http://127.0.0.1:%d" % srv.server_address[1], "RUN_TOKEN": "rt-test"}


def run():
    return subprocess.run([sys.executable, os.path.join(HERE, "run.py")], env=ENV, capture_output=True, text=True, timeout=60)


r = run()
res = [json.loads(l[len("RESULT: "):]) for l in r.stdout.splitlines() if l.startswith("RESULT: ")]
check("exits 0 and prints RESULT", r.returncode == 0 and bool(res), (r.stdout + r.stderr).strip()[-120:])
check("RESULT: tenant from the broker context, demo.ping count = 3",
      bool(res) and res[0] == {"tenant": "testcity", "demo_ping_read": 3}, str(res[:1]))
check("every request carried the run token", STATE["tokens"] == {"rt-test"}, str(STATE["tokens"]))
STATE["tenant"] = None
r = run()
check("no tenant in the broker context → fails loudly", r.returncode != 0 and "no tenant" in (r.stdout + r.stderr), "exit %s" % r.returncode)

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
