#!/usr/bin/env python3
"""Chat Engine — server logic for Workspace Chat (unified human + AI). The host invokes HANDLERS[verb](ctx):
    ctx["tenant"], ctx["sub"], ctx["body"]                 — request context
    ctx["kwrite"](id, obj) / kfind(schema, where) / kget(id)  — registry I/O (component://chat-engine obo user)
    ctx["ai_complete"](messages, max_tokens)               — model gateway (Kimi K3) completion, returns text
Each handler returns (result_dict, None) or (None, "error"). Membership-scoped: a user sees only channels they
belong to; a channel whose members include agent://ai gets an automatic AI reply (just another chat.message).
Edit in Studio → Publish → upgrade. (ADR: chat/mail logic is a certified engine, not a host bridge.)
"""
import time as _t, os as _os

AI = "agent://ai"

def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

def _mid(tn):
    return "ds:%s:chat/message/%d-%s" % (tn, int(_t.time() * 1000), _os.urandom(3).hex())

def _msg(ctx, ref, sender, body, kind):
    tn = ctx["tenant"]
    return {"schema": "chat.message", "id": _mid(tn), "channel_ref": ref, "sender": sender,
            "body": body, "kind": kind, "observed_at": _now(), "tenant": tn}

def channels(ctx):
    me = ctx["sub"]
    mine = [c for c in ctx["kfind"]("chat.channel") if me in (c.get("members") or [])]
    mine.sort(key=lambda c: c.get("last_message_at") or c.get("created_at") or "", reverse=True)
    return {"channels": mine, "me": me}, None

def create(ctx):
    me, tn, b = ctx["sub"], ctx["tenant"], ctx["body"]
    members = sorted({m for m in ([me] + (b.get("members") or [])) if m})
    kind = b.get("kind") or ("ai" if AI in members and len(members) <= 2
                             else ("dm" if len(members) == 2 else "group"))
    title = (b.get("title") or "").strip() or ("AI Assistant" if kind == "ai"
             else ", ".join(m.rsplit("/", 1)[-1] for m in members if m != me) or "New chat")
    cid = "ds:%s:chat/channel/%d-%s" % (tn, int(_t.time() * 1000), _os.urandom(3).hex())
    obj = {"schema": "chat.channel", "id": cid, "title": title, "kind": kind, "members": members,
           "created_by": me, "created_at": _now(), "last_message_at": _now(), "tenant": tn}
    ctx["kwrite"](cid, obj)
    return {"channel": obj}, None

def messages(ctx):
    me = ctx["sub"]; ref = ctx["body"].get("channel_ref")
    ch = ctx["kget"](ref)
    if not ch:
        return None, "no such channel"
    if me not in (ch.get("members") or []):
        return None, "not a member of this channel"
    msgs = sorted(ctx["kfind"]("chat.message", {"channel_ref": ref}), key=lambda m: m.get("observed_at") or "")
    return {"channel": ch, "messages": msgs, "me": me}, None

def send(ctx):
    me = ctx["sub"]; ref = ctx["body"].get("channel_ref"); text = (ctx["body"].get("body") or "").strip()
    if not ref or not text:
        return None, "channel_ref + body required"
    ch = ctx["kget"](ref)
    if not ch:
        return None, "no such channel"
    members = ch.get("members") or []
    if me not in members:
        return None, "not a member of this channel"
    out = []
    m1 = _msg(ctx, ref, me, text, "text"); ctx["kwrite"](m1["id"], m1); out.append(m1)
    if AI in members:                                    # the AI member answers automatically
        hist = sorted(ctx["kfind"]("chat.message", {"channel_ref": ref}), key=lambda m: m.get("observed_at") or "")[-20:]
        convo = [{"role": "assistant" if h.get("sender") == AI else "user", "content": (h.get("body") or "")[:2000]}
                 for h in hist if h.get("body")]
        sysmsg = ("You are the AI assistant in an Airawat Workspace chat (a group or 1:1 conversation). Reply "
                  "helpfully and concisely to the latest message, in plain text. Do not prefix your name.")
        try:
            reply = (ctx["ai_complete"]([{"role": "system", "content": sysmsg}] + convo, 900) or "").strip()
        except Exception:
            reply = "⚠ the AI assistant is unavailable right now — please try again."
        if reply:
            m2 = _msg(ctx, ref, AI, reply, "ai"); ctx["kwrite"](m2["id"], m2); out.append(m2)
    ch["last_message_at"] = _now(); ch["updated_at"] = ch["last_message_at"]
    try: ctx["kwrite"](ch["id"], ch)
    except Exception: pass
    return {"messages": out}, None

HANDLERS = {"chat-channels": channels, "chat-create": create, "chat-messages": messages, "chat-send": send}


def main():                       # dev self-test (green run gates Studio publish, INT-0004)
    print("Chat Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
