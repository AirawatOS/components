import importlib.util, json, sys, os
ROOT="/Users/manishsv/Documents/Projects/Airawat"
sys.path.insert(0, ROOT+"/components/_common")           # so `import aos` resolves (P1: SDK lives here)

def load(name, path):
    spec=importlib.util.spec_from_file_location(name, path); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

A=load("eff_orig", ROOT+"/components/engines/effectiveness-engine/run.py")       # hand-written
B=load("eff_aos",  ROOT+"/components/engines/effectiveness-engine-aos/run.py")   # SDK rewrite

H3="8a2a1072b59ffff"
def seed():
    reg={}
    def put(o): reg[o["id"]]=o
    put({"schema":"governance.decision","id":"ds:nashik:governance/decision/d1","tenant":"nashik",
         "outcome":"approved","decided_at":"2026-08-30T12:00:00Z","evidence_manifest":["ds:nashik:governance/recommendation/r1"]})
    put({"schema":"governance.recommendation","id":"ds:nashik:governance/recommendation/r1","tenant":"nashik",
         "proposed_action":"reduce-inflow","evidence":[f"ds:nashik:cell/crowd_density/{H3}-2026-08-30T10:00:00Z"]})
    put({"schema":"cell.crowd_density","id":f"ds:nashik:cell/crowd_density/{H3}-a","tenant":"nashik",
         "space":{"h3_id":H3},"density_sqm":7.003,"observed_at":"2026-08-30T10:00:00Z"})     # peak
    put({"schema":"cell.crowd_density","id":f"ds:nashik:cell/crowd_density/{H3}-b","tenant":"nashik",
         "space":{"h3_id":H3},"density_sqm":0.79,"observed_at":"2026-08-30T13:00:00Z"})       # latest
    return reg

def host_ctx(reg, body):
    return {"kfind":lambda s:[o for o in reg.values() if o.get("schema")==s],
            "kwrite":lambda i,o:reg.__setitem__(i, json.loads(json.dumps(o))),
            "kget":lambda i:reg.get(i), "tenant":"nashik","sub":"user://nashik/admin",
            "claims":{}, "body":body, "ai_complete":None, "call":None}

def strip(o):
    o=dict(o or {}); o.pop("observed_at",None); (o.get("prov") or {}).pop("at",None); return o

regA, regB = seed(), seed()
resA,errA = A.HANDLERS["assess-effectiveness"](host_ctx(regA, {}))
resB,errB = B.HANDLERS["assess-effectiveness"](host_ctx(regB, {}))
print("original :", errA, resA)
print("aos rewrite:", errB, resB)

effA=strip(next(o for o in regA.values() if o.get("schema")=="intelligence.effectiveness"))
effB=strip(next(o for o in regB.values() if o.get("schema")=="intelligence.effectiveness"))
ok_summary = (resA==resB)
ok_written = (effA==effB)
print("\n[1] verb summary identical :", ok_summary)
print("[2] written intelligence.effectiveness identical (modulo timestamp):", ok_written)
print("    direction=%s delta_pct=%s baseline=%s followup=%s" % (effB["direction"],effB["delta_pct"],effB["baseline"]["mean"],effB["followup"]["mean"]))

# --- meta auto-derivation + validation (aos check core) ---
from aos import meta as M
m = M.derive({"component_id":"ds:airawatdeveloper:engine/effectiveness-engine-aos","kind":"engine",
              "version":"0.1.0","provider":"ds:airawatdeveloper:developer","display_name":"Effectiveness Engine"})
problems = M.validate(m)
print("\n[3] meta AUTO-DERIVED from the code (reads/writes/scopes/verbs never hand-written):\n")
print("    "+M.to_yaml(m).replace("\n","\n    "))
print("[4] aos check → validation problems:", problems or "none ✅")

# --- boilerplate reduction ---
la=len(open(ROOT+"/components/engines/effectiveness-engine/run.py").read().splitlines())
lb=len(open(ROOT+"/components/engines/effectiveness-engine-aos/run.py").read().splitlines())
print("\n[5] run.py lines: original=%d  aos=%d  (SDK owns transport/dispatch/HANDLERS/meta)"%(la,lb))

allok = ok_summary and ok_written and not problems and set(m["reads"])>={"governance.decision","governance.recommendation","cell.crowd_density"} and m["writes"]==["intelligence.effectiveness"]
print("\n"+("✅ P1 PROVEN — SDK rewrite is behaviourally identical; meta derived+valid; reads/writes auto-tracked." if allok else "❌ mismatch — inspect above"))
sys.exit(0 if allok else 1)
