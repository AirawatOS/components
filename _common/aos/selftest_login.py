import sys
sys.path.insert(0, "/Users/manishsv/Documents/Projects/Airawat/components/_common")
from aos.solution import BrokerBackend

ok = True
def check(label, cond):
    global ok; print(("  ✓ " if cond else "  ✗ ") + label); ok = ok and cond

# A transport records calls; the two fake identities model EC2 ({username,tenant}) vs local ({username}).
def make(identity_kind):
    calls = []
    def t(method, url, body, token, tries=4):
        calls.append((url, dict(body or {})))
        if url.endswith("/login"):
            if identity_kind == "ec2":
                if "tenant" not in (body or {}):
                    raise RuntimeError("POST /login → 422: tenant required")      # EC2 rejects bare username
                return {"access_token": "JWT-ec2"}
            else:  # local realm-based: rejects the extra tenant field, accepts bare username
                if "tenant" in (body or {}):
                    raise RuntimeError("POST /login → 422: unexpected field 'tenant'")
                return {"access_token": "JWT-local"}
        if url.endswith("/v1/components/dev-token"):
            return {"run_token": "RUN-tok"}
        return {}
    return t, calls

print("[EC2 identity — {username, tenant}]")
t, calls = make("ec2")
b = BrokerBackend("newdelhi", "http://net", "http://kern", "http://idn", dev_user="devrick", transport=t)
login = [c for c in calls if c[0].endswith("/login")]
check("logged in with {username, tenant}", login and login[0][1] == {"username": "devrick", "tenant": "newdelhi"})
check("got the EC2 token + minted dev-token", b._id_tok == "JWT-ec2" and b._dev_tok == "RUN-tok")

print("[local identity — bare {username}] (tenant-scoped attempt fails → falls back)")
t, calls = make("local")
b = BrokerBackend("airawatdeveloper", "http://net", "http://kern", "http://idn", dev_user="devrick", transport=t)
login = [c for c in calls if c[0].endswith("/login")]
check("tried {username,tenant} then fell back to {username}",
      [c[1] for c in login] == [{"username": "devrick", "tenant": "airawatdeveloper"}, {"username": "devrick"}])
check("got the local token", b._id_tok == "JWT-local")

print("[explicit login_tenant override — promote INTO another tenant]")
t, calls = make("ec2")
b = BrokerBackend("newdelhi", "http://net", "http://kern", "http://idn", dev_user="devrick", transport=t,
                  login_tenant="airawatdeveloper")
login = [c for c in calls if c[0].endswith("/login")]
check("login uses the override tenant, not the deploy tenant",
      login and login[0][1] == {"username": "devrick", "tenant": "airawatdeveloper"})

print("[identity_token provided → no login call at all]")
t, calls = make("ec2")
b = BrokerBackend("newdelhi", "http://net", "http://kern", "http://idn", identity_token="PRESET", transport=t)
check("no /login when a token is supplied", not any(c[0].endswith("/login") for c in calls) and b._id_tok == "PRESET")

print("\n" + ("✅ BrokerBackend login PROVEN — EC2 {username,tenant} + local {username} + override + preset token."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
