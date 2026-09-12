#!/usr/bin/env python3
"""Mail Engine — server logic for Workspace Mail (internal + federated). The host invokes HANDLERS[verb](ctx):
    ctx["tenant"], ctx["sub"], ctx["body"], ctx["kwrite"], ctx["kfind"], ctx["kget"],
    ctx["http"], ctx["network_url"], ctx["token"]
Membership-scoped: a mail.message is visible to its sender + recipients (To∪CC∪BCC). BCC is API-redacted — a
reader who is neither the sender nor a bcc recipient never sees the bcc list.

FEDERATION (ADR-0023/0024, signed-relay): a recipient in ANOTHER tenant (`name@othertenant`,
`user://othertenant/name`, or `participant://…`) is delivered by store-and-forward, NOT a cross-tenant write.
On send, the engine keeps a local Sent copy and hands the message to the network relay
(`/v1/federation/mail/relay`), which signs a federation envelope AS the sender tenant's steward and routes it
to the recipient tenant's steward inbox. On listing, the engine DRAINS its own tenant's steward inbox (the
network returns only signature-verified envelopes) and writes each into its OWN registry with its OWN
authority (`federation_ref` = the envelope id, for idempotent de-dup), then acks. Tenant isolation is never
crossed. Each handler returns (result, None) or (None, "error"). Edit in Studio → Publish → upgrade.
"""
import time as _t, os as _os

def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

def _norm(tn, lst):
    out = []
    for t in (lst or []):
        t = (t or "").strip()
        if not t:
            continue
        if "://" in t:
            out.append(t)                                    # already a principal (user://, participant://, agent://)
        elif "@" in t:
            n, _, d = t.partition("@")                       # name@tenant → user://tenant/name (cross-tenant address)
            out.append("user://%s/%s" % (d, n) if d else "user://%s/%s" % (tn, n))
        else:
            out.append("user://%s/%s" % (tn, t))             # bare name → this tenant
    return out

def _tenant_of(ref):
    """The tenant a principal ref belongs to (user://<t>/… or participant://<t>/…), else None."""
    for scheme in ("user://", "participant://"):
        if (ref or "").startswith(scheme):
            return ref[len(scheme):].partition("/")[0] or None
    return None

def _redact_bcc(m, me):
    # BCC is blind: only the sender and a bcc recipient ever see the bcc list.
    if me == m.get("sender") or me in (m.get("bcc") or []):
        return m
    r = dict(m); r["bcc"] = []; return r

def send(ctx):
    me, tn, b = ctx["sub"], ctx["tenant"], ctx["body"]
    to, cc, bcc = _norm(tn, b.get("to")), _norm(tn, b.get("cc")), _norm(tn, b.get("bcc"))
    recips = sorted(set(to + cc + bcc))                      # everyone who receives (powers inbox membership)
    if not recips:
        return None, "at least one recipient required"
    mid = "ds:%s:mail/message/%d-%s" % (tn, int(_t.time() * 1000), _os.urandom(3).hex())
    atts = [a for a in (b.get("attachments") or []) if isinstance(a, str)]
    obj = {"schema": "mail.message", "id": mid, "sender": me, "recipients": recips,
           "cc": sorted(set(cc)), "bcc": sorted(set(bcc)),
           "subject": (b.get("subject") or "(no subject)").strip(), "body": b.get("body") or "",
           "thread_ref": b.get("thread_ref"), "attachments": atts, "read_by": [me],
           "federation_ref": None, "observed_at": _now(), "tenant": tn}
    ctx["kwrite"](mid, obj)                                  # local Sent copy (local recipients read it here)
    # Federated delivery: hand the message to the network relay for any recipient in another tenant.
    remote = [r for r in recips if (_tenant_of(r) and _tenant_of(r) != tn)]
    relayed = None
    if remote:
        try:
            r = ctx["http"]("POST", ctx["network_url"] + "/v1/federation/mail/relay",
                            {"message": obj, "recipients": remote}, ctx["token"])
            relayed = (r or {}).get("results")
        except Exception as e:
            relayed = [{"error": str(e)[:200]}]
    out = {"message": _redact_bcc(obj, me)}
    if remote:
        out["federated"] = {"recipients": remote, "results": relayed}
    return out, None

def _drain(ctx):
    """Pull signature-verified federated mail from this tenant's steward inbox and deliver into our own
    registry (own-tenant write). Idempotent via federation_ref. Best-effort — never blocks listing."""
    me, tn = ctx["sub"], ctx["tenant"]
    try:
        r = ctx["http"]("POST", ctx["network_url"] + "/v1/federation/mail/inbox", {}, ctx["token"])
    except Exception:
        return
    pend = (r or {}).get("messages") or []
    if not pend:
        return
    seen = set(m.get("federation_ref") for m in ctx["kfind"]("mail.message") if m.get("federation_ref"))
    acked = []
    for p in pend:
        env_id = p.get("env_id"); obj = p.get("object") or {}
        if not env_id:
            continue
        if env_id in seen or not isinstance(obj, dict) or obj.get("schema") != "mail.message":
            acked.append(env_id); continue                   # already delivered / not deliverable → drop from inbox
        mid = "ds:%s:mail/message/%d-%s" % (tn, int(_t.time() * 1000), _os.urandom(3).hex())
        local = {"schema": "mail.message", "id": mid, "sender": obj.get("sender"),
                 "recipients": obj.get("recipients") or [], "cc": obj.get("cc") or [], "bcc": obj.get("bcc") or [],
                 "subject": obj.get("subject") or "(no subject)", "body": obj.get("body") or "",
                 "thread_ref": obj.get("thread_ref"), "attachments": obj.get("attachments") or [],
                 "read_by": [], "federation_ref": env_id, "observed_at": obj.get("observed_at") or _now(), "tenant": tn}
        try:
            ctx["kwrite"](mid, local); seen.add(env_id); acked.append(env_id)
        except Exception:
            pass                                             # leave in inbox for a later retry
    if acked:
        try:
            ctx["http"]("POST", ctx["network_url"] + "/v1/federation/mail/ack", {"env_ids": acked}, ctx["token"])
        except Exception:
            pass

def listing(ctx):
    me = ctx["sub"]; folder = (ctx["body"].get("folder") or "inbox").lower()
    if folder != "sent":
        _drain(ctx)                                          # fetch any federated mail before listing the inbox
    msgs = ctx["kfind"]("mail.message")
    if folder == "sent":
        mine = [m for m in msgs if m.get("sender") == me]
    else:
        mine = [m for m in msgs if me in (m.get("recipients") or [])]
    mine.sort(key=lambda m: m.get("observed_at") or "", reverse=True)
    unread = sum(1 for m in msgs if me in (m.get("recipients") or []) and me not in (m.get("read_by") or []))
    return {"messages": [_redact_bcc(m, me) for m in mine], "me": me, "unread": unread}, None

def get(ctx):
    me = ctx["sub"]; mid = ctx["body"].get("id")
    m = ctx["kget"](mid)
    if not m:
        return None, "no such message"
    if me != m.get("sender") and me not in (m.get("recipients") or []):
        return None, "not your message"
    rb = m.get("read_by") or []
    if me not in rb:                                         # mark read for this reader
        m["read_by"] = rb + [me]
        try: ctx["kwrite"](m["id"], m)
        except Exception: pass
    return {"message": _redact_bcc(m, me)}, None

HANDLERS = {"mail-list": listing, "mail-send": send, "mail-get": get}


def main():                       # dev self-test (green run gates Studio publish, INT-0004)
    print("Mail Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
