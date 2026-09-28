import sys, os, base64, hashlib, tempfile, time, json
sys.path.insert(0, "/Users/manishsv/Documents/Projects/Airawat/components/_common")
import aos.oidc as oidc
from aos.solution import BrokerBackend

ok = True
def check(label, cond):
    global ok; print(("  ✓ " if cond else "  ✗ ") + label); ok = ok and cond

# isolate the credential cache to a temp file (never touch ~/.aos)
oidc.CRED = os.path.join(tempfile.mkdtemp(), "credentials.json")
ISS = "https://idp.example/realms/airawatos"

print("[PKCE]")
v, c = oidc._pkce()
check("challenge == base64url(sha256(verifier)), unpadded",
      c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode())

print("[cache write → read]")
oidc._cache_write(ISS, "aos-cli", {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 300})
check("cached_token returns the fresh token", oidc.cached_token(ISS) == "AT-1")
check("cache file is 0600", oct(os.stat(oidc.CRED).st_mode & 0o777) == oct(0o600))

print("[expired → refresh via refresh_token]")
d = json.load(open(oidc.CRED)); d[ISS]["expiry"] = time.time() - 10; json.dump(d, open(oidc.CRED, "w"))
calls = []
def fake_http(method, url, data=None, form=False, headers=None):
    calls.append((url, (data or {}).get("grant_type")))
    if url.endswith("/.well-known/openid-configuration"):
        return {"token_endpoint": ISS + "/token", "authorization_endpoint": ISS + "/auth",
                "device_authorization_endpoint": ISS + "/device"}
    if url.endswith("/token"):
        return {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 300}
    return {}
oidc._http = fake_http
check("expired token auto-refreshes → new access_token", oidc.cached_token(ISS) == "AT-2")
check("refresh used grant_type=refresh_token", any(g == "refresh_token" for _, g in calls))
check("refreshed token re-cached (next read is fresh, no new call)",
      (calls.clear() or oidc.cached_token(ISS)) == "AT-2" and not calls)

print("[expired + no refresh_token → None]")
d = json.load(open(oidc.CRED)); d[ISS] = {"access_token": "AT-x", "expiry": time.time() - 10}; json.dump(d, open(oidc.CRED, "w"))
check("no refresh_token → not logged in (None)", oidc.cached_token(ISS) is None)

print("[BrokerBackend precedence: cached login used when no explicit token / no local idn]")
oidc._cache_write(ISS, "aos-cli", {"access_token": "AT-live", "expires_in": 300})
os.environ["AIRAWAT_OIDC_ISSUER"] = ISS
def stub(method, url, body, token, tries=4):
    return {"run_token": "RUN"} if url.endswith("/v1/components/dev-token") else {}
b = BrokerBackend("newdelhi", "http://net", "http://kern", identity_url=None, transport=stub)  # no idn, no token
check("BrokerBackend picked up the aos-login cached token", b._id_tok == "AT-live" and b._dev_tok == "RUN")

print("[explicit identity_token still wins over cache]")
b2 = BrokerBackend("newdelhi", "http://net", "http://kern", identity_url=None, identity_token="EXPLICIT", transport=stub)
check("explicit token beats cache", b2._id_tok == "EXPLICIT")

print("[logout clears the issuer]")
oidc.logout(ISS)
check("after logout, cached_token is None", oidc.cached_token(ISS) is None)
os.environ.pop("AIRAWAT_OIDC_ISSUER", None)

print("\n" + ("✅ aos login PROVEN — PKCE, cache read/write/refresh, BrokerBackend precedence, logout."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
