#!/usr/bin/env python3
"""Authoring Engine — the AI build/design advisors, extracted out of app_host (_bridge_solution_expert +
_bridge_service_expert). Both GROUND a model prompt in the live governed catalog (marketplace components +
schemas + roles + credential types + the current project) and PROPOSE — the human enacts. They RETURN a
spec/actions; they never write to disk (that Studio orchestration — solution-generate/project-* — stays host-side).

  • solution-expert — decompose a requirement into a reuse-first component graph → {answer, actions}.
  • service-expert  — from a description, propose a complete service in the v2 offering shape (form, documents, fees,
                     workflow with SLA / zone office / certificate / record updates),
                     grounded in the GOVERNED roles + credential types → {reply, spec, new_credential_types}.

Reasoning models ramble/truncate; the JSON salvage here is ported verbatim so the UI always gets a usable result.
Uses host-held ctx capabilities: http (model /complete + network catalog), kernel_url + token (user-scoped reads).
"""
import json as _json, re as _re


def _read1(ctx, oid):
    try:
        r = ctx["http"]("POST", ctx["kernel_url"] + "/v1/registry/resolve", {"id": oid}, ctx.get("token"))
        rs = (r or {}).get("results", [])
        return rs[0]["object"] if rs else None
    except Exception:
        return None


def _complete(ctx, messages, max_tokens=900, task=None):
    body = {"messages": messages, "max_tokens": max_tokens}
    if task:
        body["task"] = task
    return ctx["http"]("POST", ctx["embed_url"] + "/complete", body)


def h_solution_expert(ctx):
    b = ctx["body"]
    message = (b.get("message") or "").strip(); history = b.get("history") or []
    if not message:
        return None, "empty message"
    net = ctx["network_url"]
    try:
        comps = ctx["http"]("GET", net + "/v1/store").get("listings", [])
    except Exception:
        comps = []
    cat = "\n".join(f"  - {c.get('component_id')} [{c.get('kind')}] {c.get('title', '')}" for c in comps if c.get("kind") != "app")[:1800]
    try:
        schemas = sorted({s.get("schema_id") for s in ctx["http"]("GET", net + "/v1/schemas").get("schemas", []) if s.get("schema_id")})
    except Exception:
        schemas = []
    proj_ctx = ""
    if b.get("project"):
        p = _read1(ctx, b["project"])
        if p:
            proj_ctx = (f"Project '{p.get('title')}' — goal: {p.get('goal')}\nMembers so far:\n"
                        + "\n".join(f"  - {m.get('ref')} [{m.get('kind')}] {m.get('status')}" for m in (p.get('members') or [])))
            br = p.get("brief") or {}
            if any(br.get(k) for k in ("users", "entities", "data_sources", "decisions", "triggers")):
                proj_ctx += ("\nBRIEF — users: " + ", ".join(br.get("users") or ["—"])
                             + "; things: " + ", ".join(br.get("entities") or ["—"])
                             + "; data sources: " + ", ".join(br.get("data_sources") or ["—"])
                             + "; decisions: " + ", ".join(br.get("decisions") or ["—"])
                             + "; triggers: " + ", ".join(br.get("triggers") or ["—"]))
    dtenant = (b.get("project") or "").split(":")[1] if (b.get("project") or "").startswith("ds:") else "airawatdeveloper"
    cat = cat[:1100]
    system = (
        "You are the AirawatOS Solution Expert. A solution is LAYERED components that talk through specs. Map the "
        "BRIEF: data sources → drivers (domain layer, fetch→facts); decisions/questions → engines (facts→facts) + "
        "skills (rules→recommendation) in the intelligence layer; users → an app (interaction layer). Set each "
        "component's reads + writes (governed schema ids) so the wiring is explicit. REUSE existing marketplace "
        "components/schemas first; only scaffold new when nothing fits. Action types: "
        '{"type":"add_component","ref":"<marketplace id>","kind":"...","title":"...","reason":"..."} | '
        '{"type":"request_schema","schema_id":"domain.subject","version":"0.1.0","x_kind":"entity|measure|event|record","attributes":"field_a,field_b,field_c","reason":"..."} | '
        f'{{"type":"scaffold","component":"ds:{dtenant}:<kind>/<name>","kind":"driver|engine|skill","reads":"<schema,schema>","writes":"<schema>","title":"...","reason":"..."}}\n'
        "Every NEW schema MUST be classified with x_kind (entity=a thing with identity/geometry; measure=a "
        "derived cell indicator — also name x_measures_of the entity it views; event=something happened; "
        "record=bookkeeping). One subject × rung × kind = one noun — never propose a duplicate of a governed schema.\n"
        "For request_schema, ALWAYS fill `attributes` — the comma-separated DOMAIN field names the noun actually "
        "carries (its real content), inferred from the brief. Do NOT list envelope fields: Studio auto-adds "
        "schema, id and tenant to every noun, observed_at to every measure/event, and prov+subject to measures.\n"
        f"MARKETPLACE (reuse these):\n{cat or '  (none)'}\nGOVERNED SCHEMAS: {', '.join(schemas) or '(none)'}\n{proj_ctx}")
    msgs = [{"role": "system", "content": system}]
    for h in history[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})
    msgs.append({"role": "user", "content": message +
        '\n\nRespond with ONLY one minified JSON object, starting with { and ending with }, no other text, '
        'no markdown, no reasoning: {"answer":"AT MOST 2 short sentences","actions":[...]}. 3-6 actions.'})
    try:
        res = _complete(ctx, msgs, 900)
    except Exception as e:
        return None, "the Solution Expert's model is unavailable: " + str(e)
    content = (res.get("content") or "").strip()
    answer, actions = "", []
    i, jx = content.find("{"), content.rfind("}")
    if i >= 0 and jx > i:
        try:
            obj = _json.loads(content[i:jx + 1])
            answer = (obj.get("answer") or "").strip()
            actions = obj.get("actions") if isinstance(obj.get("actions"), list) else []
        except Exception:
            pass
    if not answer:
        ma = _re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', content, _re.S)
        if ma:
            try: answer = _json.loads('"' + ma.group(1) + '"')
            except Exception: answer = ma.group(1)
    if not actions:
        for am in _re.findall(r'\{[^{}]*"type"\s*:\s*"(?:add_component|request_schema|scaffold)"[^{}]*\}', content):
            try: actions.append(_json.loads(am))
            except Exception: pass
    if not answer:
        answer = _re.sub(r'^(the user wants?|the requirement|let me|we need|okay|first,).*?(?=\n|$)', '', content, flags=_re.I).strip() or content
    return {"answer": answer, "actions": actions, "model": res.get("model")}, None


_V2_EXAMPLE = {   # a complete, working service in the v2 shape (City Development Authority — Name transfer), trimmed
  "service_id": "city-da-name-transfer", "title": "Name transfer (mutation) of a property", "domain": "urban",
  "description": "Record a property in the new holder's name after a sale, inheritance, gift or family settlement.",
  "required_credentials": ["credential.identity_verified"], "scope_map": {"area": "zone"}, "fee_ref": "urban.fee_schedule",
  "fulfillment": {"fee_service": "name-transfer", "payment": "before_submit",
    "documents": [{"type": "ownership-proof", "when": {}}, {"type": "sale-deed", "when": {"transfer_reason": "sale"}},
                  {"type": "death-certificate", "when": {"transfer_reason": "inheritance"}}],
    "sla_hours": {"scrutiny": 72, "approval": 48},
    "workflow": [{"step": "scrutiny", "role": "urban.dealing-assistant", "office_by": "zone", "task": "Check the application, documents and dues."},
                 {"step": "approval", "role": "urban.estate-officer", "task": "Approve; the order is issued to the applicant.",
                  "issues": "credential.urban_name_transfer", "effects": "transfer_holding:transferee_name"}]},
  "request_schema": {"type": "object", "required": ["property", "transfer_reason", "transferee_name"],
    "x-order": ["property", "transfer_reason", "transferor_name", "transferee_name", "deed_no", "remarks"],
    "properties": {
      "property": {"type": "string", "title": "Unified Property ID", "x-section": "Property", "x-lookup": "urban.property"},
      "transfer_reason": {"type": "string", "title": "Reason for transfer", "x-section": "Transfer", "enum": ["sale", "inheritance", "gift"]},
      "transferor_name": {"type": "string", "title": "Present holder (as recorded)"},
      "transferee_name": {"type": "string", "title": "New holder (your name)", "x-prefill": "identity.name"},
      "deed_no": {"type": "string", "title": "Registered deed number", "x-show-if": {"transfer_reason": ["sale", "gift"]}},
      "remarks": {"type": "string", "title": "Anything the office should know", "x-widget": "textarea", "x-section": "Other"}}}}


def _v2_sanitize(spec, roles, ctypes, doc_codes, fee_codes):
    """Keep the model honest: drop anything not in the governed / department vocabulary, and say what was dropped."""
    notes = []
    if not isinstance(spec, dict):
        return {}, notes
    ff = spec.get("fulfillment") if isinstance(spec.get("fulfillment"), dict) else {}
    wf = []
    for st in ff.get("workflow") or []:
        if not isinstance(st, dict):
            continue
        if st.get("role") not in roles:
            notes.append("step '%s': role '%s' is not governed — choose one in the Workflow tab" % (st.get("step"), st.get("role")))
            st["role"] = ""
        if st.get("issues") and st["issues"] not in ctypes:
            notes.append("'%s' is not an approved credential type — removed" % st["issues"]); st.pop("issues")
        eff = st.get("effects")
        if isinstance(eff, str) and eff.startswith("transfer_holding:"):
            fld = eff.split(":", 1)[1]
            prop = next((k for k, p in ((spec.get("request_schema") or {}).get("properties") or {}).items() if isinstance(p, dict) and p.get("x-lookup") == "urban.property"), None)
            if prop:
                st["effects"] = [{"close": {"schema": "urban.property_holding", "match": {"property": "$form." + prop}, "patch": {"until": "$today"}}},
                                 {"write": {"schema": "urban.property_holding", "property": "$form." + prop, "holder_name": "$form." + fld,
                                            "holder_ref": "$applicant", "relation": "owner", "share_pct": 100, "since": "$today", "source_doc": "$case"}}]
            else:
                st.pop("effects")
        elif eff == "convert_freehold":
            prop = next((k for k, p in ((spec.get("request_schema") or {}).get("properties") or {}).items() if isinstance(p, dict) and p.get("x-lookup") == "urban.property"), None)
            if prop:
                st["effects"] = [{"close": {"schema": "urban.property", "match": {"id": "$form." + prop}, "patch": {"tenure": "freehold"}}},
                                 {"close": {"schema": "urban.lease", "match": {"property": "$form." + prop, "status": "active"}, "patch": {"status": "converted"}}}]
            else:
                st.pop("effects")
        elif eff is not None:
            st.pop("effects")
        wf.append(st)
    ff["workflow"] = wf
    docs = []
    for d in ff.get("documents") or []:
        if isinstance(d, dict) and d.get("type") in doc_codes:
            docs.append({"type": d["type"], "when": d.get("when") if isinstance(d.get("when"), dict) else {}})
        elif isinstance(d, dict):
            notes.append("document type '%s' is not in your department's list — removed" % d.get("type"))
    ff["documents"] = docs
    if ff.get("fee_service") and ff["fee_service"] not in fee_codes:
        notes.append("no fee rules exist yet for code '%s' — add them in the Fees tab" % ff["fee_service"])
    spec["fulfillment"] = ff
    spec["required_credentials"] = [c for c in (spec.get("required_credentials") or []) if c in ctypes]
    return spec, notes


def h_service_expert(ctx):
    """Service Studio AI Expert (v2): propose a COMPLETE service in the v2 offering shape — sectioned form with choices,
    conditions, property lookup; document checklist; fee rules + payment; workflow with SLA, zone office, certificate and
    record updates — grounded in the governed roles/credential types and the department's own document types and fee
    codes (sent by the Studio). The proposal is sanitised against that vocabulary before it reaches the editor."""
    b = ctx["body"]
    msg = (b.get("message") or "").strip()
    history = b.get("history") or []
    current = b.get("spec") or {}
    vocab = b.get("vocab") or {}
    net = ctx["network_url"]
    roles, ctypes = [], []
    try: roles = sorted({x["role"] for x in (ctx["http"]("GET", net + "/v1/roles").get("roles") or []) if "." in (x.get("role") or "")})
    except Exception: pass
    try: ctypes = sorted({s["schema_id"] for s in (ctx["http"]("GET", net + "/v1/schemas").get("schemas") or []) if str(s.get("schema_id", "")).startswith("credential.")})
    except Exception: pass
    doc_codes = {d.get("code"): d.get("name") for d in (vocab.get("document_types") or []) if d.get("code")}
    fee_codes = {k: v for k, v in (vocab.get("fee_codes") or {}).items()}
    urban_roles = [r for r in roles if r.startswith(("urban.", "civic."))] or roles
    prompt = ("You are the Service Studio AI Expert. An urban department's admin designs a citizen service with NO code. "
        "Propose the COMPLETE service as JSON in exactly the shape of this working example (same keys, same conventions):\n"
        + _json.dumps(_V2_EXAMPLE) + "\n\nConventions:\n"
        "- request_schema: field keys snake_case; list them in x-order. A field is {type:'string'|'number', title, ...}; format:'date' for dates; "
        "x-widget:'textarea' for long text; enum:[...] (+ optional x-enum-labels) for choices; x-section starts a heading; "
        "x-show-if:{<choice field>: value or [values]} shows a field only for those answers; x-prefill:'identity.name' for the applicant's own name.\n"
        "- If the service is about an existing property, include ONE field with x-lookup:'urban.property' (the Unified Property ID) and set scope_map {area:'zone'} "
        "so it routes to the zone office; do NOT ask for address, area, land use or zone — the lookup fills them.\n"
        "- fulfillment.documents: [{type:<code>, when:{<choice field>: value(s)} or {}}] using ONLY these document codes: " + _json.dumps(doc_codes) + "\n"
        "- Fees: if the service charges a fee set fee_ref:'urban.fee_schedule' and fulfillment.fee_service to one of this department's fee codes "
        + _json.dumps(fee_codes) + " (or a new kebab-case code if none fits — the admin will add its rules); payment:'before_submit' when it is paid online.\n"
        "- fulfillment.workflow: ordered steps {step:'<kebab>', role:<ONE OF " + _json.dumps(urban_roles) + ">, task, office_by:'zone' (only with a property lookup), "
        "issues:<credential type, ONLY from " + _json.dumps(ctypes) + ">, effects:'transfer_holding:<field with the new holder name>' only when the holder changes, or 'convert_freehold' when a leasehold property becomes freehold}; "
        "fulfillment.sla_hours {step: hours} for every step.\n"
        "- required_credentials: usually ['credential.identity_verified'].\n"
        "Respond with ONE JSON object only — no reasoning, no markdown: {\"reply\":\"<2-3 sentences: what you built and what the admin should check>\", \"spec\":{...}}.\n\n"
        "Current editor state (may be empty): " + _json.dumps(current)[:1800] + "\n\nConversation so far:\n"
        + "\n".join(f"{h.get('role','user')}: {h.get('text','')}" for h in history[-6:]) + "\nuser: " + msg)
    try:
        res = _complete(ctx, [{"role": "user", "content": prompt}], 6000, task="chat")
    except Exception as e:
        return None, "the Service Expert model is unavailable — try again: " + str(e)[:120]
    content = res.get("content") or ""
    content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.S)
    content = _re.sub(r"<think>.*$", "", content, flags=_re.S)
    m = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, _re.S) or _re.search(r"(\{.*\})", content, _re.S)
    out = None
    if m:
        try: out = _json.loads(m.group(1))
        except Exception: out = None
    if not isinstance(out, dict) or not (out.get("spec") or out.get("reply")):
        return {"reply": "I couldn't structure that into a service yet — try one or two sentences: what the citizen applies for, "
                         "which property or record it concerns, who checks and who approves, whether there is a fee, and what is issued.",
                "spec": {}, "model": res.get("model")}, None
    spec, notes = _v2_sanitize(out.get("spec") or {}, set(roles), set(ctypes), set(doc_codes), set(fee_codes))
    reply = out.get("reply") if isinstance(out.get("reply"), str) else ""
    if len(reply) > 700:
        reply = reply[:700].rstrip() + "…"
    return {"reply": reply, "spec": spec, "notes": notes, "shape": "v2", "model": res.get("model")}, None


HANDLERS = {"solution-expert": h_solution_expert, "service-expert": h_service_expert}


def main():
    print("Authoring Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
