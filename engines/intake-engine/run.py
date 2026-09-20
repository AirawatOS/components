#!/usr/bin/env python3
"""Intake Engine — the channel-neutral civic-intake BRAIN, extracted out of app_host (`_intake_core`).

Given a normalised message {tenant, sender, text, source, lat?, lng?} from ANY channel (sms / whatsapp / ussd /
web), it: (1) classifies the citizen's words with the model gateway into {intent, category, severity, one_line,
place, service_query} over the governed intake codelist; (2) ROUTES — a `service_request` opens a governed service
ENQUIRY case (found via the service directory), a `grievance`/`info` becomes a `signal.incident` at the citizen
tier; (3) shapes the record(s) and a human REPLY.

It returns a WRITE-SET (not direct writes) because civic channels are UNAUTHENTICATED — the records must be
persisted under the tenant's service identity, which the host holds; the engine owns the LOGIC, the host owns the
privileged write. Pure domain logic here; no per-channel code, no secrets.
"""
import hashlib as _h, json as _json, re as _re, time as _t

DEFAULT_CATS = ["law_and_order", "water", "sanitation", "health", "roads_transport", "environment",
                "infrastructure", "governance", "disaster", "welfare", "education", "other"]
_INTENTS = ("grievance", "service_request", "info")
_SEV = ("low", "medium", "high", "critical")


def _now():
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())


def _categories(ctx, tenant):
    """Governed-first: the tenant's (else platform) `ref.codelist/civic-intake-category` items; else the default.
    Best-effort — a channel with no session token reads nothing and falls back to the engine's default list."""
    try:
        for oid in (f"ds:{tenant}:ref/codelist/civic-intake-category", "ds:airawat:ref/codelist/civic-intake-category"):
            rec = ctx["kget"](oid)
            items = (rec or {}).get("items")
            if isinstance(items, list) and items:
                return items
    except Exception:
        pass
    return DEFAULT_CATS


def h_intake_process(ctx):
    """Classify → route → shape. Returns {writes:[{id,object}], reply, intent, category, ...} for the host to
    persist under the tenant service identity."""
    b = ctx["body"]
    tenant = b.get("tenant"); sender = b.get("sender"); text = (b.get("text") or "").strip()
    source = b.get("source") or "message"; lat = b.get("lat"); lng = b.get("lng")
    if not tenant or not text:
        return None, "tenant + text required"
    CATS = _categories(ctx, tenant)

    parsed = {}
    try:
        out = ctx["ai_complete"]([{"role": "user", "content":
            "A citizen contacted their city over " + source + ". Return ONLY JSON {intent (grievance|service_request|info), "
            "category (one of: " + ", ".join(CATS) + "), severity (low|medium|high|critical), one_line (<=12 words), "
            "service_query (2-4 words naming the government service requested, e.g. birth certificate / water connection, or null), "
            "place (a locality/landmark named in the text, or null)}.\nMessage: " + text}], 300)
        m = _re.search(r"\{.*\}", out or "", _re.S)
        parsed = _json.loads(m.group(0)) if m else {}
    except Exception:
        parsed = {}
    intent = parsed.get("intent") if parsed.get("intent") in _INTENTS else "grievance"
    cat = parsed.get("category") if parsed.get("category") in CATS else "other"
    sev = str(parsed.get("severity") or "low").lower()
    if sev not in _SEV: sev = "low"
    one = (parsed.get("one_line") or text)[:200]
    place = parsed.get("place") if isinstance(parsed.get("place"), str) else None
    sq = parsed.get("service_query") if isinstance(parsed.get("service_query"), str) else None
    now = _now()
    ref = _h.sha1((str(sender) + "|" + text + "|" + now[:16]).encode()).hexdigest()[:12]
    reporter = ("%s-sha256:%s" % (source, _h.sha256((tenant + ":" + str(sender)).encode()).hexdigest()[:16])) if sender else (source + "-anonymous")

    if intent == "service_request":                          # ADR-0067: a service request → the service spine, not an incident
        off = None
        try:
            sr = ctx["http"]("POST", ctx["network_url"] + "/v1/service/search", {"intent": {"query": (sq or one or text)}}) or {}
            cat0 = (sr.get("message") or {}).get("catalog") or []
            cand = [o for o in cat0 if o.get("tenant") == tenant] or cat0    # prefer this tenant's offerings
            off = cand[0] if cand else None
        except Exception:
            off = None
        if off:
            case_id = "ds:%s:case/svc-%s" % (tenant, ref)
            case = {"schema": "platform.case", "id": case_id, "tenant": tenant, "status": "open",
                    "title": ((off.get("title") or off.get("service_id")) + " - citizen request via " + source)[:120],
                    "opened_at": now, "opened_by": reporter, "current_step": "enquiry",
                    "service": off.get("service_id"), "subjects": [off.get("id")]}
            svc = off.get("title") or off.get("service_id")
            reply = "Opened a service request for %s. Ref %s. A service agent will contact you to complete it (ID proof may be needed)." % (svc, ref)
            return {"writes": [{"id": case_id, "object": case}], "kind": "service_case", "id": case_id, "ref": ref,
                    "intent": intent, "service": off.get("service_id"), "offering": off.get("id"), "reply": reply}, None
        # no offering found → fall through to an incident (never lose the request)

    iid = "ds:%s:signal/incident/%s-%s" % (tenant, source, ref)
    rec = {"schema": "signal.incident", "id": iid, "tenant": tenant, "title": one, "jurisdiction": tenant,
           "reporter_kind": "citizen", "reporter": reporter, "source": source, "status": "open",
           "observed_at": now, "category": cat, "severity": sev, "description": text}
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)): rec["lat"] = lat; rec["lng"] = lng
    if place: rec["location_text"] = place
    hint = " A service agent will guide you." if intent == "service_request" else ""
    reply = "Registered as %s (%s). Ref %s.%s Reply STATUS %s for updates." % (cat, intent, ref, hint, ref)
    return {"writes": [{"id": iid, "object": rec}], "id": iid, "ref": ref, "intent": intent, "category": cat,
            "severity": sev, "located": ("lat" in rec), "reply": reply}, None


HANDLERS = {"intake-process": h_intake_process}


def main():
    print("Intake Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
