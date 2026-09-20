#!/usr/bin/env python3
"""Authoring Engine — the AI build/design advisors, extracted out of app_host (_bridge_solution_expert +
_bridge_service_expert). Both GROUND a model prompt in the live governed catalog (marketplace components +
schemas + roles + credential types + the current project) and PROPOSE — the human enacts. They RETURN a
spec/actions; they never write to disk (that Studio orchestration — solution-generate/project-* — stays host-side).

  • solution-expert — decompose a requirement into a reuse-first component graph → {answer, actions}.
  • service-expert  — from a description, propose a complete service spec (form + workflow + credentials),
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


def h_service_expert(ctx):
    b = ctx["body"]
    msg = (b.get("message") or "").strip()
    history = b.get("history") or []
    current = b.get("spec") or {}
    net = ctx["network_url"]
    roles, ctypes = [], []
    try: roles = sorted({x["role"] for x in (ctx["http"]("GET", net + "/v1/roles").get("roles") or []) if "." in (x.get("role") or "")})
    except Exception: pass
    try: ctypes = sorted({s["schema_id"] for s in (ctx["http"]("GET", net + "/v1/schemas").get("schemas") or []) if str(s.get("schema_id", "")).startswith("credential.")})
    except Exception: pass
    prompt = ("You are the Service Studio AI Expert for a civic-services platform. A city-agency admin is designing a "
        "government service with NO code. From their description + conversation, propose a COMPLETE service spec as JSON.\n"
        "Spec fields: service_id (kebab-case), title, domain (civic|urban|safety|health|finance|mobility), description, "
        "entitlement (residence|property), request_schema {type:object, additionalProperties:false, properties:{<key>:{title,type:'string'|'number', format?:'date'}}, required:[keys]} = the citizen intake FORM, "
        "workflow: ordered [{step:'<kebab>', role:'<GOVERNED role>', task:'<what the officer does>', issues?:'<GOVERNED credential type minted at this step>'}], "
        "required_credentials: [<GOVERNED credential types the citizen must present>].\n"
        f"GOVERNED ROLES you may use (use ONLY these): {roles}\n"
        f"GOVERNED CREDENTIAL TYPES you may use (use ONLY these for issues/required_credentials): {ctypes}\n"
        "If the service needs a credential TYPE not in the governed list, DO NOT invent it in the spec — instead list it under "
        "new_credential_types:[{type:'credential.<snake_case>', title, claims:[field names]}] and note in your reply that it must be network-approved first.\n"
        "Respond with the JSON object ONLY — no reasoning, no <think> blocks, no markdown fences, no prose outside it: "
        "{\"reply\":\"<one short helpful paragraph>\", \"spec\":{...}, \"new_credential_types\":[...]}.\n\n"
        "Current spec (may be partial/empty): " + _json.dumps(current)[:1400] + "\n\nConversation so far:\n"
        + "\n".join(f"{h.get('role','user')}: {h.get('text','')}" for h in history[-6:]) + "\nuser: " + msg)
    try:
        res = _complete(ctx, [{"role": "user", "content": prompt}], 4000, task="chat")
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
        out = {"reply": "I couldn't structure that into a service yet — try one or two sentences: what the citizen applies for, who reviews it, and whether it issues a document (e.g. \"Businesses apply for a trade licence; a clerk verifies documents, a sanitation officer inspects the premises, then issue a trade-licence certificate\")."}
    if isinstance(out.get("reply"), str) and len(out["reply"]) > 700:
        out["reply"] = out["reply"][:700].rstrip() + "…"
    return out, None


HANDLERS = {"solution-expert": h_solution_expert, "service-expert": h_service_expert}


def main():
    print("Authoring Engine — handlers:", ", ".join(sorted(HANDLERS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
