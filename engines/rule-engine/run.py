#!/usr/bin/env python3
"""RULE ENGINE (kind=engine) — ADR-0088 lane 1. A GENERIC evaluator: given a `ruleset` name + a bag of `facts`,
it loads the governed `governance.policy_rule` records for that ruleset and returns the matching `then` effect(s) —
plus a trace of which rules fired. It DECIDES, it does not ACT (a pure function over governed config), so the
same engine backs any policy: data-visibility, access classification, scope containment, thresholds, gates.

Host dispatch: /bridge/rule-eval → HANDLERS["rule-eval"](ctx). ctx["kfind"]("governance.policy_rule", …) reads the
rules under the caller's token (tenant-scoped); the effect object is opaque — the caller interprets it.

Local proof (no stack): `python run.py --selftest` runs the built-in truth table.
"""
import os, re

# The FIXED, safe operator set — a matcher, NOT a language. No eval/exec, no attribute access, no I/O here.
def _leaf(node, facts):
    v = facts.get(node["field"]); op = node["op"]; want = node.get("value")
    if op == "eq":       return v == want
    if op == "ne":       return v != want
    if op == "lt":       return v is not None and v <  want
    if op == "lte":      return v is not None and v <= want
    if op == "gt":       return v is not None and v >  want
    if op == "gte":      return v is not None and v >= want
    if op == "in":       return v in (want or [])
    if op == "nin":      return v not in (want or [])
    if op == "contains": return want in (v or [])
    if op == "exists":   return (v is not None) == bool(want)
    if op == "regex":    return bool(v is not None and re.search(str(want), str(v)))
    raise ValueError(f"unknown operator '{op}'")


def match(node, facts):
    """Evaluate a boolean predicate TREE against a flat facts dict. Total (never raises on missing facts)."""
    if not node:              return True                 # empty `when` ⇒ always matches
    if "all" in node:         return all(match(n, facts) for n in node["all"])
    if "any" in node:         return any(match(n, facts) for n in node["any"])
    if "not" in node:         return not match(node["not"], facts)
    return _leaf(node, facts)


def evaluate(rules, facts, mode="first"):
    """Core: sort enabled rules by priority (lower first), find matches, return decision + trace.
    mode='first' → the first matching rule's effect (or None); mode='all' → list of all matching effects."""
    ordered = sorted([r for r in rules if r.get("enabled", True)], key=lambda r: r.get("priority", 100))
    hits = [r for r in ordered if match(r.get("when", {}), facts)]
    trace = [r.get("id") for r in hits]
    if mode == "all":
        decision = [r.get("then") for r in hits]
    else:
        decision = hits[0].get("then") if hits else None
    return {"matched": len(hits), "decision": decision, "trace": trace}


# ---- host handler ---------------------------------------------------------------------------------------
def h_rule_eval(ctx):
    body = ctx["body"]
    ruleset = body.get("ruleset")
    if not ruleset:
        return None, "ruleset required"
    facts = body.get("facts") or {}
    mode = body.get("mode", "first")
    rules = ctx["kfind"]("governance.policy_rule", {"ruleset": ruleset})    # governed config, read under the caller's token
    out = evaluate(rules, facts, mode)
    out["ruleset"] = ruleset
    return out, None


HANDLERS = {"rule-eval": h_rule_eval}


def _selftest():
    RULES = [
        {"id": "r/hide-sensitive", "ruleset": "data.visibility", "priority": 100, "enabled": True,
         "when": {"all": [{"field": "sensitivity", "op": "eq", "value": "sensitive"}]},
         "then": {"effect": "deny", "label": "hidden"}},
        {"id": "r/allow-public", "ruleset": "data.visibility", "priority": 200, "enabled": True,
         "when": {"any": [{"field": "domain", "op": "eq", "value": "ref"},
                          {"field": "schema", "op": "eq", "value": "viz.layer"}]},
         "then": {"effect": "allow", "label": "public"}},
    ]
    cases = [
        ({"sensitivity": "sensitive", "domain": "hr"},   "deny",  ["r/hide-sensitive"]),
        ({"sensitivity": "internal", "domain": "ref"},   "allow", ["r/allow-public"]),
        ({"sensitivity": "internal", "domain": "geo"},   None,    []),
        ({"schema": "viz.layer", "sensitivity": "internal"}, "allow", ["r/allow-public"]),
    ]
    ok = True
    for facts, want_eff, want_trace in cases:
        r = evaluate(RULES, facts, "first")
        got_eff = (r["decision"] or {}).get("effect") if r["decision"] else None
        pass_ = (got_eff == want_eff and r["trace"] == want_trace)
        ok = ok and pass_
        print(f"  [{'ok' if pass_ else 'FAIL'}] facts={facts} → effect={got_eff} trace={r['trace']}")
    # operator coverage
    ops = [({"field":"n","op":"gt","value":5}, {"n":9}, True), ({"field":"n","op":"gt","value":5}, {"n":2}, False),
           ({"field":"t","op":"in","value":["a","b"]}, {"t":"b"}, True), ({"field":"t","op":"regex","value":"^ab"}, {"t":"abc"}, True),
           ({"field":"x","op":"exists","value":True}, {}, False), ({"field":"arr","op":"contains","value":"k"}, {"arr":["k","j"]}, True)]
    for node, facts, want in ops:
        got = match(node, facts); pass_ = got == want; ok = ok and pass_
        print(f"  [{'ok' if pass_ else 'FAIL'}] {node['op']}({facts}) = {got}")
    print("SELFTEST", "PASS" if ok else "FAIL"); return 0 if ok else 1


def main():
    if os.environ.get("RULE_SELFTEST") or ("--selftest" in os.sys.argv):
        return _selftest()
    print("rule-engine — handler: rule-eval (dev self-test: --selftest)")
    return 0


if __name__ == "__main__":
    import sys as _s; raise SystemExit(main())
