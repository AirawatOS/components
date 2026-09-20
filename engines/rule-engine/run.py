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


def _leaf_count(node):
    """Specificity of a `when` tree = number of leaf predicates. More constraints ⇒ more specific.
    Empty/absent `when` = 0 (the least-specific 'default' rule)."""
    if not node:                    return 0
    if "all" in node:               return sum(_leaf_count(n) for n in node["all"])
    if "any" in node:               return sum(_leaf_count(n) for n in node["any"])
    if "not" in node:               return _leaf_count(node["not"])
    return 1


def evaluate(rules, facts, mode="first", consumer_tenant=None):
    """Core resolver. Modes:
      first          — flat priority order (lower first), first matching rule's effect (or None). [S1]
      all            — every matching effect.
      most_specific  — of the matching rules, the one with the MOST `when` constraints wins; ties broken by
                       tenant-owned > platform, then priority. This generalizes org-hierarchy policy resolution
                       (policy-engine's 'most-specific-first' by applies_to key count) without hardcoding a domain."""
    enabled = [r for r in rules if r.get("enabled", True)]
    if mode == "most_specific":
        matches = [r for r in enabled if match(r.get("when", {}), facts)]
        # rank: more constraints first; a tenant-owned rule beats a platform default at equal specificity; then priority.
        def _own(r): return 1 if (r.get("tenant") and r.get("owner") != "platform") else 0
        ranked = sorted(matches, key=lambda r: (_leaf_count(r.get("when", {})), _own(r), -r.get("priority", 100)), reverse=True)
        winner = ranked[0] if ranked else None
        return {"matched": len(matches), "decision": (winner or {}).get("then") if winner else None,
                "trace": [winner["id"]] if winner else [],
                "winning_scope": (winner or {}).get("when") if winner else None,
                # ADR-0091 V2: expose the full ranked match set (most-specific first) so a Decision-plane alias
                # (e.g. policy-resolve) can reconstruct its policy LIST from the generic evaluator's output.
                "ranked": [{"id": r.get("id"), "then": r.get("then"), "when": r.get("when"),
                            "priority": r.get("priority")} for r in ranked]}
    ordered = sorted(enabled, key=lambda r: r.get("priority", 100))
    hits = [r for r in ordered if match(r.get("when", {}), facts)]
    trace = [r.get("id") for r in hits]
    decision = [r.get("then") for r in hits] if mode == "all" else (hits[0].get("then") if hits else None)
    return {"matched": len(hits), "decision": decision, "trace": trace}


# ---- host handler ---------------------------------------------------------------------------------------
def h_rule_eval(ctx):
    body = ctx["body"]
    ruleset = body.get("ruleset")
    if not ruleset:
        return None, "ruleset required"
    facts = dict(body.get("facts") or {})
    # ADR-0088-S2: a governance.ruleset descriptor may declare the resolution strategy + a context provider.
    desc = next((d for d in ctx["kfind"]("governance.ruleset") if d.get("ruleset") == ruleset and d.get("enabled", True)), None)
    mode = body.get("mode") or (desc or {}).get("resolution") or "first"
    # CONTEXT PROVIDER: if the ruleset declares one and the caller passes a `subject`, derive facts from it
    # (e.g. an employee's classification). The org-tree logic lives in that provider engine, not here.
    subject = body.get("subject")
    cprov = (desc or {}).get("context")
    if subject and cprov and cprov.get("engine") and cprov.get("verb") and ctx.get("call"):
        try:
            res, err = ctx["call"](cprov["engine"], cprov["verb"], {"subject": subject, "employee": subject})
            if not err and isinstance(res, dict):
                facts.update(res.get("context") or res.get("facts") or res)   # merge derived facts
        except Exception as e:
            facts["_context_error"] = str(e)[:120]
    rules = ctx["kfind"]("governance.policy_rule", {"ruleset": ruleset})       # governed config, read under the caller's token
    out = evaluate(rules, facts, mode, consumer_tenant=ctx.get("tenant"))
    out["ruleset"] = ruleset; out["resolution"] = mode; out["facts"] = facts
    return out, None


# ADR-0091: `evaluate` is the CANONICAL Decision-plane verb; `rule-eval` is retained as a legacy alias.
HANDLERS = {"evaluate": h_rule_eval, "rule-eval": h_rule_eval}


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
    # most_specific: the leave-policy shape (default 12; manager rank>=5 → 18). The tighter `when` wins.
    LEAVE = [
        {"id": "r/leave-default", "ruleset": "hr.leave_policy", "when": {}, "then": {"leave_days": 12}},
        {"id": "r/leave-manager", "ruleset": "hr.leave_policy",
         "when": {"all": [{"field": "rank", "op": "gte", "value": 5}]}, "then": {"leave_days": 18}},
    ]
    for facts, want_days, want_win in [({"rank": 6}, 18, "r/leave-manager"), ({"rank": 2}, 12, "r/leave-default")]:
        r = evaluate(LEAVE, facts, "most_specific")
        got = (r["decision"] or {}).get("leave_days"); pass_ = (got == want_days and r["trace"] == [want_win])
        ok = ok and pass_
        print(f"  [{'ok' if pass_ else 'FAIL'}] most_specific rank={facts['rank']} → {got} days (winner {r['trace']})")
    print("SELFTEST", "PASS" if ok else "FAIL"); return 0 if ok else 1


def main():
    if os.environ.get("RULE_SELFTEST") or ("--selftest" in os.sys.argv):
        return _selftest()
    print("rule-engine — handler: rule-eval (dev self-test: --selftest)")
    return 0


if __name__ == "__main__":
    import sys as _s; raise SystemExit(main())
