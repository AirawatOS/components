#!/usr/bin/env python3
"""Mail Engine — server logic for Workspace Mail (internal/federated). The host invokes HANDLERS[verb](ctx):
    ctx["tenant"], ctx["sub"], ctx["body"], ctx["kwrite"], ctx["kfind"], ctx["kget"]
Membership-scoped: a mail.message is visible to its sender + recipients. Each handler returns (result, None) or
(None, "error"). Edit in Studio → Publish → upgrade. (ADR: chat/mail logic is a certified engine, not a host bridge.)
"""
import time as _t, os as _os

def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

def send(ctx):
    me, tn, b = ctx["sub"], ctx["tenant"], ctx["body"]
    recips = []
    for t in (b.get("to") or []):
        t = (t or "").strip()
        if not t:
            continue
        recips.append(t if "://" in t else "user://%s/%s" % (tn, t))
    recips = sorted(set(recips))
    if not recips:
        return None, "at least one recipient required"
    mid = "ds:%s:mail/message/%d-%s" % (tn, int(_t.time() * 1000), _os.urandom(3).hex())
    obj = {"schema": "mail.message", "id": mid, "sender": me, "recipients": recips,
           "subject": (b.get("subject") or "(no subject)").strip(), "body": b.get("body") or "",
           "thread_ref": b.get("thread_ref"), "read_by": [me], "observed_at": _now(), "tenant": tn}
    ctx["kwrite"](mid, obj)
    return {"message": obj}, None

def listing(ctx):
    me = ctx["sub"]; folder = (ctx["body"].get("folder") or "inbox").lower()
    msgs = ctx["kfind"]("mail.message")
    if folder == "sent":
        mine = [m for m in msgs if m.get("sender") == me]
    else:
        mine = [m for m in msgs if me in (m.get("recipients") or [])]
    mine.sort(key=lambda m: m.get("observed_at") or "", reverse=True)
    unread = sum(1 for m in msgs if me in (m.get("recipients") or []) and me not in (m.get("read_by") or []))
    return {"messages": mine, "me": me, "unread": unread}, None

def get(ctx):
    me = ctx["sub"]; mid = ctx["body"].get("id")
    m = ctx["kget"](mid)
    if not m:
        return None, "no such message"
    if me != m.get("sender") and me not in (m.get("recipients") or []):
        return None, "not your message"
    rb = m.get("read_by") or []
    if me not in rb:                                     # mark read for this reader
        m["read_by"] = rb + [me]
        try: ctx["kwrite"](m["id"], m)
        except Exception: pass
    return {"message": m}, None

HANDLERS = {"mail-list": listing, "mail-send": send, "mail-get": get}
