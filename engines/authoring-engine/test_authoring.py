import importlib.util, os, sys
spec=importlib.util.spec_from_file_location("ae", os.path.join(os.path.dirname(os.path.abspath(__file__)),"run.py"))
ae=importlib.util.module_from_spec(spec); spec.loader.exec_module(ae)
def ctx(body, complete):
    def http(method,url,b=None,tok=None):
        if url.endswith("/complete"): return {"content": complete, "model":"kimi"}
        if url.endswith("/store"): return {"listings":[{"component_id":"ds:x:driver/d","kind":"driver","title":"D"}]}
        if url.endswith("/schemas"): return {"schemas":[{"schema_id":"credential.birth"},{"schema_id":"geo.building"}]}
        if url.endswith("/roles"): return {"roles":[{"role":"civic.clerk"}]}
        if "registry/resolve" in url: return {"results":[]}
        return {}
    return {"body":body,"http":http,"network_url":"http://n","kernel_url":"http://k","embed_url":"http://e","token":"T"}
ok=True
def chk(n,c):
    global ok; print(("  PASS " if c else "  FAIL ")+n); ok=ok and c
# 1) solution-expert clean JSON
r,e=ae.h_solution_expert(ctx({"message":"track floods"}, '{"answer":"Use a flood driver.","actions":[{"type":"add_component","ref":"ds:x:driver/d"}]}'))
chk("1 answer parsed", r["answer"]=="Use a flood driver.")
chk("1 action parsed", r["actions"][0]["ref"]=="ds:x:driver/d")
# 2) solution-expert salvage from truncated JSON
r,e=ae.h_solution_expert(ctx({"message":"x"}, 'blah {"answer":"Partial answer here","actions":[{"type":"scaffold","component":"ds:d:driver/z"}] and then it rambled'))
chk("2 salvaged answer", "Partial answer" in r["answer"])
chk("2 salvaged action", any(a.get("type")=="scaffold" for a in r["actions"]))
# 3) service-expert clean spec
r,e=ae.h_service_expert(ctx({"message":"trade licence"}, '{"reply":"Here is a trade licence service","spec":{"service_id":"trade-licence"},"new_credential_types":[]}'))
chk("3 spec parsed", r["spec"]["service_id"]=="trade-licence")
chk("3 reply parsed", "trade licence" in r["reply"])
# 4) service-expert garbage → helpful fallback
r,e=ae.h_service_expert(ctx({"message":"x"}, 'let me think about this... no json'))
chk("4 fallback reply on garbage", "couldn't structure" in r["reply"])
# 5) service-expert strips <think>
r,e=ae.h_service_expert(ctx({"message":"x"}, '<think>reasoning</think>{"reply":"ok","spec":{"service_id":"s"}}'))
chk("5 think stripped, spec parsed", r.get("spec",{}).get("service_id")=="s")
print("\nRESULT:", "ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
