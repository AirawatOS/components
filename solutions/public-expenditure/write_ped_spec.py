"""Fold the PED data model into a governed platform.service_spec under the janaagraha tenant (ADR-0109).
Reuse-first nouns + the extractor-as-driver + the citizen app as ONE governed unit. Writer = the janaagraha
steward (passwordless service principal); falls back to janaagraha-dev (password) for the dev tenant."""
import json, os, urllib.request, urllib.error
IDN="http://identity:8093"; KERNEL="http://kernel:8080"
SPEC_ID="ds:janaagraha:service_spec/public-expenditure-dashboard"; TENANT="janaagraha"
def post(u,b,t=None):
    h={"Content-Type":"application/json"}
    if t:h["Authorization"]="Bearer "+t
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(b).encode(),headers=h,method="POST"),timeout=25).read() or b"{}"), None
    except urllib.error.HTTPError as e: return None, f"{e.code} {e.read().decode()[:200]}"

# janaagraha-tenant writer
tok=(post(IDN+"/realms/%s/login"%TENANT,{"username":"steward"})[0] or {}).get("access_token")
if not tok:
    tok=(post(IDN+"/realms/%s/login"%TENANT,{"username":"janaagraha-dev","password":"Airawat@123"})[0] or {}).get("access_token")
print("writer token:", bool(tok))

R="(reuse — already governed)"
spec={
 "constitution":"ds:airawat:constitution/default",
 "domain_model":[
  {"noun":"geo.ward","status":"built","role":"ward / zone boundary the road + work resolve to "+R,"impl":"geo.ward"},
  {"noun":"geo.osm_feature","status":"built","role":"raw OSM road geometry a segment is matched to "+R,"impl":"geo.osm_feature"},
  {"noun":"knowledge.document","status":"built","role":"every supporting PDF (BOQ, completion cert, agreement, tender notice, bill, MB, QC) + citations "+R,"impl":"knowledge.document"},
  {"noun":"finance.transaction","status":"built","role":"optional double-entry ledger a payment posts to "+R,"impl":"finance.transaction"},
  {"noun":"geo.road","status":"roadmap","role":"the named street a citizen picks — the anchor","impl":"driver: road-matcher (OSM)"},
  {"noun":"geo.road_segment","status":"roadmap","role":"a surveyed segment (start/end, length) a work touches","impl":"driver: road-matcher"},
  {"noun":"works.work_order","status":"roadmap","role":"the core road work: sanction, type, roads, completion","impl":"driver: works-register-extractor (Work Order PDFs)"},
  {"noun":"procurement.tender","status":"roadmap","role":"the tender preceding the work","impl":"driver: ka-eprocurement"},
  {"noun":"works.contract","status":"roadmap","role":"awarded contract: value, agreement, defect-liability period","impl":"driver: works-register-extractor"},
  {"noun":"works.contractor","status":"roadmap","role":"the contractor (party)","impl":"driver: works-register-extractor"},
  {"noun":"works.payment","status":"roadmap","role":"what was actually paid vs the contract","impl":"driver: works-register-extractor"},
  {"noun":"civic.issue","status":"built","role":"a concern a resident raises on a work "+R,"impl":"civic.issue"},
 ],
 "operations":[
  {"verb":"lifecycle.request","noun":"civic.issue","transition":"raise-concern","status":"roadmap","role":"a resident flags a work (overpaid, incomplete, within liability)"},
  {"verb":"lifecycle.decide","noun":"civic.issue","transition":"respond","status":"roadmap","role":"the responsible official responds / resolves"},
  {"verb":"lifecycle.status","noun":"civic.issue","transition":"track","status":"roadmap","role":"anyone tracks the concern"},
 ],
 "apps":[
  {"app":"ped-explorer","status":"roadmap","role":"citizen map — pick a road, drill into every work: cost, contractor, payment, DLP, documents (City Scan geo layer)"},
  {"app":"ped-ingest-console","status":"roadmap","role":"admin — run/monitor the extractor + review low-confidence road/tender links"},
 ],
 "workflow":{
  "ingestion":{"status":"roadmap","states":["extracted","roads_linked","tender_payment_reconciled","published"],"impl":"drivers: works-register-extractor → road-matcher → ka-eprocurement"},
  "accountability":{"status":"roadmap","states":["raised","acknowledged","resolved","rejected"],"impl":"civic.issue via the lifecycle engine"},
 },
 "roles":[
  {"role":"civic.resident","status":"built","role_desc":"views works on a road; raises concerns "+R},
  {"role":"civic.rwa","status":"roadmap","role_desc":"resident welfare association — neighbourhood oversight"},
  {"role":"civic.ward-committee-member","status":"roadmap","role_desc":"ward committee — local accountability"},
  {"role":"works.engineer","status":"roadmap","role_desc":"the municipal official who answers a concern"},
 ],
 "business_rules":[
  {"id":"PED-SOURCE-ATTRIBUTION","status":"roadmap","statement":"every displayed figure carries its source (work register / tender portal) + extraction confidence (prov+confidence)"},
  {"id":"PED-ROAD-LINK","status":"roadmap","statement":"a work is shown on a road only if road_key_confidence ≥ threshold; below it the work is flagged 'unlinked' for review"},
  {"id":"PED-DLP-ACTIVE","status":"roadmap","statement":"a work whose today ∈ [dlp_from, dlp_to] is flagged 'under contractor liability'"},
  {"id":"PED-PAYMENT-FLAG","status":"roadmap","statement":"paid_gross > contract_value flags the work 'overpaid'; net_vs_contract surfaces the gap"},
  {"id":"PED-PII-EXCLUDE","status":"roadmap","statement":"documents carrying PII are excluded from public view (n_documents_excluded_pii recorded on prov)"},
 ],
 "acceptance_criteria":[
  {"id":"AC-ROAD-DRILL","status":"roadmap","test":None,"given":"a road with recorded works","when":"a citizen opens it in the PED","then":"each work shows cost, contractor, payment-vs-contract, DLP status and links to its documents","traces":["PED-SOURCE-ATTRIBUTION","PED-DLP-ACTIVE"]},
  {"id":"AC-PROVENANCE","status":"roadmap","test":None,"given":"any figure on the dashboard","when":"a citizen inspects it","then":"its source + confidence are shown","traces":["PED-SOURCE-ATTRIBUTION"]},
  {"id":"AC-OVERPAY-FLAG","status":"roadmap","test":None,"given":"a work paid above its contract value","when":"it is ingested","then":"the work is flagged 'overpaid'","traces":["PED-PAYMENT-FLAG"]},
  {"id":"AC-RAISE-CONCERN","status":"roadmap","test":None,"given":"a resident viewing a work","when":"they raise a concern","then":"a civic.issue (lifecycle.request) is routed to the responsible official","traces":[]},
 ],
 "nfr":[
  {"id":"NFR-PUBLIC-READ","status":"roadmap","statement":"viewing is public / citizen-facing; raising a concern needs a civic login"},
  {"id":"NFR-ATTRIBUTED","status":"built","statement":"every write attributed to the acting driver/engine on-behalf-of (ADR-0052)"},
  {"id":"NFR-INGEST-SOURCES","status":"roadmap","statement":"two sources on the critical path — BBMP work register (via the Work-Order-PDF extractor) + Karnataka eProcurement — each a governed driver with declared egress"},
 ],
}
obj={"schema":"platform.service_spec","id":SPEC_ID,
     "capability":"Track public money to the neighbourhood level: for any city road, show every public work on it — cost, contractor, tender, payment, defect-liability status and the documents behind it — sourced from the municipal work register + the state tender portal, so citizens, RWAs and ward committees can see and question how public money is spent.",
     "version":"0.1.0","realized_by":"ds:janaagraha:solution/public-expenditure-dashboard","status":"roadmap",
     "spec":spec,"source_yaml":"components/solutions/public-expenditure/ped-data-model.md"}
_,we=post(KERNEL+"/v1/registry/objects",{"id":SPEC_ID,"object":obj},tok)
print("write PED service_spec:","OK" if not we else "ERR "+str(we))
r,_=post(KERNEL+"/v1/registry/resolve",{"id":SPEC_ID},tok)
o=((r.get("results") or [{}])[0].get("object") or {}) if r else {}
sp=o.get("spec") or {}
print("  capability:", (o.get("capability") or "")[:60])
print("  nouns=%d ops=%d apps=%d workflows=%d rules=%d acceptance=%d roles=%d" % (
  len(sp.get("domain_model",[])),len(sp.get("operations",[])),len(sp.get("apps",[])),
  len(sp.get("workflow",{})),len(sp.get("business_rules",[])),len(sp.get("acceptance_criteria",[])),len(sp.get("roles",[]))))
