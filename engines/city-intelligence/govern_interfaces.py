"""Govern the City Intelligence VERB as a managed network.interface (ADR-0058/0063) so `city.analyst` is a
first-class MANAGED VERB in the Network catalogue (Network Admin → Verbs) — the request/response contract the
city-intelligence engine implements, and the target the conformance gate (ADR-0090) checks. Steward-gated,
idempotent. Run inside a container that can reach the network + identity."""
import json, os, urllib.request as u, urllib.error
NET=os.environ.get("NET","http://airawat-network:8090"); IDN=os.environ.get("IDN","http://airawat-identity:8093"); VER="0.1.0"
def post(url,body,hdr=None):
    r=u.Request(url,data=json.dumps(body).encode(),headers={"Content-Type":"application/json",**(hdr or {})},method="POST")
    try: return json.loads(u.urlopen(r,timeout=60).read()),None
    except urllib.error.HTTPError as e: return None,f"{e.code}:{e.read().decode()[:240]}"
def obj(req,props):
    d={"type":"object","properties":props}
    if req: d["required"]=req
    return d
INTERFACES={
 "city.analyst": {
   "modality":"request","x-actor":"operator",
   "description":"City Intelligence — grounded Q&A over a tenant's governed map layers. Given an operator's "
                 "question, the bound engine reads the live layers (risk.flood, cell.crowd_density, "
                 "vision.detection, governance.recommendation), computes the cross-layer facts (e.g. cells that "
                 "are both flooding and crowded), and returns a cited answer grounded ONLY on those records. The "
                 "AI informs; a human commander decides (ADR-0029).",
   "request": obj(["question"],{
       "question":{"type":"string","description":"the operator's natural-language question about the city layers"}}),
   "response": obj(["answer"],{
       "answer":{"type":"string","description":"the grounded briefing, cited to governed records"},
       "sources":{"type":"array","items":{"type":"string"},"description":"the layer schemas the answer draws on"},
       "facts":{"type":"object","description":"the computed cross-layer facts the answer is grounded on"}}),
   "roles":[]},
}
def main():
    r,e=post(f"{IDN}/realms/airawat/login",{"username":"steward"})
    if not r: print("LOGIN FAILED:",e); return
    STOK="Bearer "+r["access_token"]; ok=0
    for iid,defn in INTERFACES.items():
        res,err=post(f"{NET}/v1/interfaces",{"interface_id":iid,"version":VER,"definition":defn},{"Authorization":STOK})
        if res: ok+=1; print(f"  ✓ {iid}@{VER} ({res['published']['modality']})")
        else: print(f"  ✗ {iid}: {err}")
    cat=json.loads(u.urlopen(f"{NET}/v1/interfaces",timeout=20).read())["interfaces"]
    print("city.* in catalog:", sorted(i["interface_id"] for i in cat if i["interface_id"].startswith("city.")))
main()
