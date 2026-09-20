"""Parity test for the ported intake brain (stub ctx: fake ai_complete + http + kget). Asserts h_intake_process
reproduces app_host._intake_core's classify→route→shape logic: grievance→signal.incident write-set, service_request
→service-search→platform.case, category/severity normalisation, native lat/lng, anonymous reporter."""
import importlib.util, os, sys, json

spec = importlib.util.spec_from_file_location("ie", os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py"))
ie = importlib.util.module_from_spec(spec); spec.loader.exec_module(ie)

def ctx(body, ai=None, catalog=None, codelist=None):
    def _ai(msgs, mx=300): return ai
    def _http(method, url, b=None, tok=None):
        if "service/search" in url: return {"message": {"catalog": catalog or []}}
        return {}
    def _kget(oid): return {"items": codelist} if codelist else None
    return {"body": body, "ai_complete": _ai, "http": _http, "network_url": "http://net", "kget": _kget}

ok = True
def chk(n, c):
    global ok; print(("  PASS " if c else "  FAIL ") + n); ok = ok and c

# 1) grievance → signal.incident write-set
r, e = ie.h_intake_process(ctx({"tenant": "kmc", "sender": "+9199", "text": "Overflowing drain near market", "source": "sms"},
    ai='{"intent":"grievance","category":"sanitation","severity":"high","one_line":"Drain overflow at market","place":"market"}'))
chk("1 no err", e is None)
w = (r.get("writes") or [{}])[0].get("object", {})
chk("1 writes a signal.incident", w.get("schema") == "signal.incident")
chk("1 category=sanitation", w.get("category") == "sanitation")
chk("1 severity=high", w.get("severity") == "high")
chk("1 location_text from place", w.get("location_text") == "market")
chk("1 reporter is salted hash (not raw phone)", w.get("reporter", "").startswith("sms-sha256:") and "+9199" not in w.get("reporter", ""))
chk("1 intent + reply returned", r.get("intent") == "grievance" and "Ref" in r.get("reply", ""))

# 2) bad category/severity from the model → normalised to 'other'/'low'
r, e = ie.h_intake_process(ctx({"tenant": "kmc", "sender": "x", "text": "hello", "source": "sms"},
    ai='{"intent":"grievance","category":"NONSENSE","severity":"apocalyptic","one_line":"hi"}'))
w = (r.get("writes") or [{}])[0].get("object", {})
chk("2 bad category → other", w.get("category") == "other")
chk("2 bad severity → low", w.get("severity") == "low")

# 3) service_request + a matching offering → platform.case (not incident)
r, e = ie.h_intake_process(ctx({"tenant": "kmc", "sender": "x", "text": "I need a birth certificate", "source": "whatsapp"},
    ai='{"intent":"service_request","service_query":"birth certificate","category":"governance","severity":"low","one_line":"Birth cert"}',
    catalog=[{"tenant": "kmc", "service_id": "birth-cert", "title": "Birth Certificate", "id": "ds:kmc:service_offering/birth-cert"}]))
chk("3 kind=service_case", r.get("kind") == "service_case")
w = (r.get("writes") or [{}])[0].get("object", {})
chk("3 writes a platform.case", w.get("schema") == "platform.case")
chk("3 case.service=birth-cert", w.get("service") == "birth-cert")
chk("3 reply mentions the service", "Birth Certificate" in r.get("reply", ""))

# 4) service_request but NO offering → falls back to an incident (never lose it)
r, e = ie.h_intake_process(ctx({"tenant": "kmc", "sender": "x", "text": "need a unicorn licence", "source": "sms"},
    ai='{"intent":"service_request","service_query":"unicorn licence","category":"other","severity":"low","one_line":"x"}', catalog=[]))
w = (r.get("writes") or [{}])[0].get("object", {})
chk("4 falls back to signal.incident", w.get("schema") == "signal.incident")

# 5) native location pin → lat/lng on the record
r, e = ie.h_intake_process(ctx({"tenant": "kmc", "sender": "x", "text": "here", "source": "whatsapp", "lat": 26.4, "lng": 80.3},
    ai='{"intent":"grievance","category":"roads_transport","severity":"medium","one_line":"pothole"}'))
w = (r.get("writes") or [{}])[0].get("object", {})
chk("5 lat/lng carried", w.get("lat") == 26.4 and w.get("lng") == 80.3)

# 6) governed codelist honoured (category only valid if in the list)
r, e = ie.h_intake_process(ctx({"tenant": "kmc", "sender": "x", "text": "x", "source": "sms"},
    ai='{"intent":"grievance","category":"custom_cat","severity":"low","one_line":"x"}', codelist=["custom_cat", "other"]))
w = (r.get("writes") or [{}])[0].get("object", {})
chk("6 governed codelist category accepted", w.get("category") == "custom_cat")

print("\nRESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
