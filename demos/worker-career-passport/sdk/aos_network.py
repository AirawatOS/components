#!/usr/bin/env python3
"""aos_network 0.2 — the network SDK for a participant's SERVERS (ADR-0135 Mode B, ADR-0136 linking, ADR-0137 participants
without AirawatOS).

A platform such as Ola uses this from its own backend. It does NOT need AirawatOS: it registers on the network's portal
with its own public key, and the SDK signs in with the private key, which never leaves the platform's servers.

    # once, on your servers: make your key pair; publish keys/jwks.json at your site (or paste it in the portal)
    $ python3 aos_network.py keys generate --kid ola-2026-1 --out keys/

    from aos_network import Network
    net = Network("https://airos.airawat.org", client_id="ola-cabs", private_key="keys/private-key.json")

    # at your own Aadhaar check (optional, recommended): keep the sealed token with your KYC record
    sealed = net.sealed_token("eshram", aadhaar_number)          # use the number in this call only

    # when a member taps "Link eShram": sign your work certificate with YOUR key and start the link
    cert = net.sign_certificate("eshram", {"full_name": "...", "mobile": "...", "aadhaar_last4": "...", ...})
    req = net.create_link_request("eshram", return_url="https://partners.example/eshram/return",
                                  role_label="Partner", certificate=cert, sealed=sealed)
    # -> open req["screen_url"] in the system browser (Custom Tabs / ASWebAuthenticationSession / a redirect)

    # back at return_url?link=<request_id>&status=…  — ask the network, don't trust the address
    st = net.link_status(req["request_id"])        # {"status": "linked", "link_id": "…"}

    # your notification endpoint (registered in the portal): events are signed with the NETWORK's key
    if net.verify_event(raw_body, headers): event = json.loads(raw_body)

A participant hosted on AirawatOS can instead pass token_provider=… (its platform service login), as in 0.1.
Never put the private key, a token or Aadhaar numbers in a mobile app: this library is for the platform's servers.
Requires: cryptography (for Ed25519)."""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.2.0"


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class NetworkError(Exception):
    def __init__(self, status, message):
        super().__init__("%s %s" % (status, message))
        self.status, self.message = status, message


def _load_private_key(private_key):
    """A private-key.json written by `keys generate` (path or dict) -> (Ed25519PrivateKey, kid)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    d = private_key
    if isinstance(private_key, str):
        with open(private_key) as fh:
            d = json.load(fh)
    return Ed25519PrivateKey.from_private_bytes(_unb64u(d["d"])), d["kid"]


class Network:
    def __init__(self, base_url, token_provider=None, client_id=None, private_key=None, timeout=20):
        self.root = base_url.rstrip("/")
        self.base = self.root + "/embed/network/v1/"
        self.timeout = timeout
        self.client_id = client_id
        self._key = self._kid = None
        self._tok = (None, 0)
        self._net_keys = (None, 0)
        if private_key is not None:
            if not client_id:
                raise ValueError("client_id is required with private_key")
            self._key, self._kid = _load_private_key(private_key)
            self.token_provider = self._token
        elif token_provider is not None:
            self.token_provider = token_provider
        else:
            raise ValueError("give private_key + client_id (your own keys) or token_provider (a hosted participant)")

    # ---- signing in with your own key (OAuth client credentials with a private-key JWT, RFC 7523) ----------------------
    def _jwt(self, payload):
        head = {"alg": "EdDSA", "typ": "JWT", "kid": self._kid}
        si = _b64u(json.dumps(head, separators=(",", ":")).encode()) + "." + _b64u(json.dumps(payload, separators=(",", ":")).encode())
        return si + "." + _b64u(self._key.sign(si.encode()))

    def _token(self):
        tok, exp = self._tok
        if tok and exp - 60 > time.time():
            return tok
        now = int(time.time())
        assertion = self._jwt({"iss": self.client_id, "sub": self.client_id, "aud": self.base + "token",
                               "iat": now, "exp": now + 120, "jti": _b64u(os.urandom(12))})
        r = self._call("token", {"grant_type": "client_credentials",
                                 "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                                 "client_assertion": assertion}, auth=False)
        self._tok = (r["access_token"], now + int(r.get("expires_in") or 300))
        return r["access_token"]

    def _call(self, path, body, auth=True):
        h = {"Content-Type": "application/json"}
        if auth:
            h["Authorization"] = "Bearer " + self.token_provider()
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers=h)
        try:
            return json.loads(urllib.request.urlopen(req, timeout=self.timeout).read())
        except urllib.error.HTTPError as e:
            try:
                d = json.loads(e.read())
                msg = d.get("error") or e.reason
                if d.get("error_description"):                 # 0.2.1: keep the network's explanation
                    msg = "%s: %s" % (msg, d["error_description"])
            except Exception:
                msg = e.reason
            raise NetworkError(e.code, msg)

    _post = _call

    # ---- the network API ------------------------------------------------------------------------------------------------
    def sealed_token(self, network, aadhaar):
        """An opaque duplicate-check token for `network`; you cannot reverse it or compare it across platforms."""
        return self._call("identity/sealed-token", {"network": network, "aadhaar": aadhaar})["sealed"]

    def register_notifications(self, network, url):
        """Hosted participants: register (or rotate) an https endpoint for events; returns an HMAC secret.
        Participants with their own keys register the endpoint in the portal and verify with verify_event()."""
        return self._call("participants/notifications", {"network": network, "url": url})["secret"]

    def sign_certificate(self, network, claims, type="credential.platform_work_certificate", title=None):
        """Your work certificate, signed with YOUR key — the network checks it against the keys you registered."""
        if not self._key:
            raise ValueError("sign_certificate needs your private key (hosted participants pass {'type', 'claims'} instead)")
        st = {"type": type, "network": network, "issuer": self.client_id, "claims": claims, "title": title,
              "iat": int(time.time()), "jti": _b64u(os.urandom(12))}
        raw = json.dumps(st, separators=(",", ":"), sort_keys=True).encode()
        return {"type": type, "statement": _b64u(raw), "signature": _b64u(self._key.sign(raw)), "kid": self._kid}

    def create_link_request(self, network, return_url, certificate, role_label=None, sealed=None, link_id=None):
        """Start linking a member: returns {request_id, screen_url}. Pass link_id to reopen an existing link."""
        body = {"network": network, "return_url": return_url, "certificate": certificate, "role_label": role_label}
        if sealed:
            body["sealed"] = sealed
        if link_id:
            body["link_id"] = link_id
        return self._call("link-requests", body)

    def link_status(self, request_id):
        """{status: pending|linked|cancelled, link_id?} — the link id is yours alone; it does not identify the person elsewhere."""
        return self._call("link-requests/status", {"request_id": request_id})

    # ---- events from the network ----------------------------------------------------------------------------------------
    def network_keys(self):
        keys, at = self._net_keys
        if keys is None or time.time() - at > 600:
            keys = json.loads(urllib.request.urlopen(self.base + "jwks", timeout=self.timeout).read()).get("keys") or []
            self._net_keys = (keys, time.time())
        return keys

    def verify_event(self, raw_body, headers):
        """True when an event carries the network's Ed25519 signature (X-Network-Signature-Ed25519 + X-Network-Key-Id)."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        g = {k.lower(): v for k, v in dict(headers).items()}
        sig, kid = g.get("x-network-signature-ed25519"), g.get("x-network-key-id")
        for k in self.network_keys():
            if k.get("kid") == kid and k.get("crv") == "Ed25519":
                try:
                    Ed25519PublicKey.from_public_bytes(_unb64u(k["x"])).verify(_unb64u(sig or ""), raw_body)
                    return True
                except Exception:
                    return False
        return False

    @staticmethod
    def verify(raw_body, signature, secret):
        """Hosted participants: True when an event body carries the HMAC signature (hex HMAC-SHA256 with your secret)."""
        good = hmac.new(bytes.fromhex(secret), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, signature or "")


def generate_keys(kid, out_dir):
    """Write out_dir/private-key.json (mode 600 — keep it on your servers) and out_dir/jwks.json (publish it)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.generate()
    x = _b64u(sk.public_key().public_bytes_raw())
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "private-key.json")
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps({"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": x, "d": _b64u(sk.private_bytes_raw())}).encode())
    os.close(fd)
    jw = {"keys": [{"kty": "OKP", "crv": "Ed25519", "kid": kid, "x": x, "use": "sig", "alg": "EdDSA"}]}
    with open(os.path.join(out_dir, "jwks.json"), "w") as fh:
        json.dump(jw, fh, indent=2)
    return jw


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:2] == ["keys", "generate"] and "--kid" in a and "--out" in a:
        jw = generate_keys(a[a.index("--kid") + 1], a[a.index("--out") + 1])
        print("wrote private-key.json (keep it secret) and jwks.json (publish it):")
        print(json.dumps(jw, indent=2))
    else:
        print("usage: python3 aos_network.py keys generate --kid <key-id> --out <dir>")
        sys.exit(2)
