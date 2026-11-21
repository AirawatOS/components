#!/usr/bin/env python3
"""aos_network — the network SDK for a participant's SERVERS (ADR-0135 Mode B, ADR-0136 linking).

A platform such as Ola uses this from its own backend to link its members to their eShram account on the network:

    from aos_network import Network
    net = Network("https://airos.airawat.org", token_provider=lambda: my_service_token())

    # at the platform's own Aadhaar check (optional, recommended): keep the sealed token with your KYC record
    sealed = net.sealed_token("livelihood", aadhaar_number)          # use the number in this call only

    # once: where the network sends events, and the secret to check them with (store the secret server-side)
    secret = net.register_notifications("livelihood", "https://partners.example/network/events")

    # when a member taps "Link eShram"
    req = net.create_link_request("livelihood", return_url="https://partners.example/eshram/return",
                                  role_label="Partner",
                                  certificate={"type": "credential.platform_work_certificate", "claims": {...}},
                                  sealed=sealed)
    # -> open req["screen_url"] in the system browser (Custom Tabs / ASWebAuthenticationSession / a redirect)

    # back at return_url?link=<request_id>&status=…  — ask the network, don't trust the address
    st = net.link_status(req["request_id"])        # {"status": "linked", "link_id": "…"}

    # your notification endpoint
    if Network.verify(raw_body, headers["X-Network-Signature"], secret): event = json.loads(raw_body)

Staging (airos.airawat.org) serves this API at /embed/network/v1/… ; the token is the participant's service login.
Never put the token, the secret or Aadhaar numbers in a mobile app: this library is for the platform's servers."""
import hashlib
import hmac
import json
import urllib.error
import urllib.request


class NetworkError(Exception):
    def __init__(self, status, message):
        super().__init__("%s %s" % (status, message))
        self.status, self.message = status, message


class Network:
    def __init__(self, base_url, token_provider, timeout=20):
        self.base = base_url.rstrip("/") + "/embed/network/v1/"
        self.token_provider = token_provider
        self.timeout = timeout

    def _post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token_provider()})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=self.timeout).read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error")
            except Exception:
                msg = e.reason
            raise NetworkError(e.code, msg)

    def sealed_token(self, network, aadhaar):
        """An opaque duplicate-check token for `network`; you cannot reverse it or compare it across platforms."""
        return self._post("identity/sealed-token", {"network": network, "aadhaar": aadhaar})["sealed"]

    def register_notifications(self, network, url):
        """Register (or rotate) your https endpoint for network events; returns the signing secret."""
        return self._post("participants/notifications", {"network": network, "url": url})["secret"]

    def create_link_request(self, network, return_url, certificate, role_label=None, sealed=None, link_id=None):
        """Start linking a member: returns {request_id, screen_url}. Pass link_id to reopen an existing link."""
        body = {"network": network, "return_url": return_url, "certificate": certificate, "role_label": role_label}
        if sealed:
            body["sealed"] = sealed
        if link_id:
            body["link_id"] = link_id
        return self._post("link-requests", body)

    def link_status(self, request_id):
        """{status: pending|linked|cancelled, link_id?} — the link id is yours alone; it does not identify the person elsewhere."""
        return self._post("link-requests/status", {"request_id": request_id})

    @staticmethod
    def verify(raw_body, signature, secret):
        """True when an event body carries the network's signature (hex HMAC-SHA256 with your secret)."""
        good = hmac.new(bytes.fromhex(secret), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, signature or "")
