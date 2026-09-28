"""aos.oidc — `aos login`: browser OIDC for the CLI, so a developer authenticates against a real
(Keycloak/OIDC) network WITHOUT pasting tokens — the pattern gh / Claude Code use.

Authorization Code + PKCE over a loopback redirect (opens the browser, captures the code on
127.0.0.1); a Device Authorization Grant fallback for headless shells. The access_token (+ refresh_token)
is cached at ~/.aos/credentials.json (0600, keyed by issuer); `aos deploy --live` and BrokerBackend read it
automatically and refresh it when expired. Precedence downstream: explicit AIRAWAT_IDENTITY_TOKEN → this
cached login → the local passwordless dev /login. Stdlib only.
"""
import base64, hashlib, http.server, json, os, secrets, threading, time, urllib.error, urllib.parse, urllib.request

CRED = os.path.expanduser("~/.aos/credentials.json")


def _issuer(issuer=None):
    from . import config
    return (issuer or config.get("oidc_issuer") or "https://airos.airawat.org/auth/realms/airawatos").rstrip("/")


def _client(client_id=None):
    from . import config
    return client_id or config.get("oidc_client_id") or "aos-cli"


def _http(method, url, data=None, form=False, headers=None):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    h = dict(headers or {})
    if form:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def discover(issuer=None):
    """OIDC discovery — the IdP advertises its authorize/token/device endpoints."""
    return _http("GET", _issuer(issuer) + "/.well-known/openid-configuration")


def _pkce():
    v = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def _cache_write(issuer, client_id, tok):
    os.makedirs(os.path.dirname(CRED), exist_ok=True)
    data = {}
    if os.path.exists(CRED):
        try:
            data = json.load(open(CRED))
        except Exception:
            data = {}
    data[issuer] = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                    "expiry": time.time() + int(tok.get("expires_in", 300)) - 30, "client_id": client_id}
    with open(CRED, "w") as f:
        json.dump(data, f)
    os.chmod(CRED, 0o600)


def cached_token(issuer=None, client_id=None):
    """A valid access_token from the cache — refreshed via refresh_token if expired. None = not logged in."""
    issuer = _issuer(issuer)
    if not os.path.exists(CRED):
        return None
    try:
        e = (json.load(open(CRED)) or {}).get(issuer)
    except Exception:
        return None
    if not e:
        return None
    if e.get("expiry", 0) > time.time():
        return e["access_token"]
    if e.get("refresh_token"):                                  # transparent refresh
        try:
            meta = discover(issuer)
            cid = e.get("client_id") or _client(client_id)
            tok = _http("POST", meta["token_endpoint"], form=True, data={
                "grant_type": "refresh_token", "refresh_token": e["refresh_token"], "client_id": cid})
            _cache_write(issuer, cid, tok)
            return tok["access_token"]
        except Exception:
            return None
    return None


def login(issuer=None, client_id=None, timeout=180):
    """Browser Authorization-Code + PKCE over a loopback redirect. Returns the access_token (also cached)."""
    issuer, client_id = _issuer(issuer), _client(client_id)
    meta = discover(issuer)
    verifier, challenge = _pkce()
    box = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            box["code"] = (p.get("code") or [None])[0]
            box["error"] = (p.get("error") or [None])[0]
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<h2>aos: login complete \xe2\x80\x94 you can close this tab.</h2>")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}/callback"
    url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect,
        "scope": "openid profile", "code_challenge": challenge, "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(16)})
    print(f"aos login: opening your browser to authenticate…\n  {url}")
    import webbrowser
    webbrowser.open(url)
    th = threading.Thread(target=srv.handle_request); th.start(); th.join(timeout); srv.server_close()
    if box.get("error"):
        raise RuntimeError(f"login failed: {box['error']}")
    if not box.get("code"):
        raise RuntimeError("login timed out — no authorization code received on the loopback callback")
    tok = _http("POST", meta["token_endpoint"], form=True, data={
        "grant_type": "authorization_code", "code": box["code"], "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier})
    _cache_write(issuer, client_id, tok)
    return tok["access_token"]


def device_login(issuer=None, client_id=None):
    """Device Authorization Grant — headless fallback (no loopback/browser control)."""
    issuer, client_id = _issuer(issuer), _client(client_id)
    meta = discover(issuer)
    dev = _http("POST", meta["device_authorization_endpoint"], form=True,
                data={"client_id": client_id, "scope": "openid profile"})
    print(f"aos login: visit {dev.get('verification_uri_complete') or dev['verification_uri']} "
          f"and enter code {dev['user_code']}")
    interval = int(dev.get("interval", 5))
    while True:
        time.sleep(interval)
        try:
            tok = _http("POST", meta["token_endpoint"], form=True, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": dev["device_code"], "client_id": client_id})
            _cache_write(issuer, client_id, tok)
            return tok["access_token"]
        except urllib.error.HTTPError as e:
            err = (json.loads(e.read() or b"{}") or {}).get("error")
            if err in ("authorization_pending", "slow_down"):
                continue
            raise


def logout(issuer=None):
    issuer = _issuer(issuer)
    if os.path.exists(CRED):
        try:
            data = json.load(open(CRED))
            data.pop(issuer, None)
            json.dump(data, open(CRED, "w"))
        except Exception:
            pass
    return issuer
