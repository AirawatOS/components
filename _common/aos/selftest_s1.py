import sys; sys.path.insert(0,__import__("os").path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))), "_common"))
from aos.solution import Solution, Deployer, InMemoryBackend
def _has_yaml():
    try:
        import yaml; return True
    except ImportError:
        return False
D=__import__("os").path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))), "solutions", "water-safety-aos")
sol=Solution.load(D)
print("parsed:", ("pyyaml" if _has_yaml() else "fallback"),
      "| solution:",sol.name,"env:",sol.environment)
print("  schemas:",[ (s.get('id') if isinstance(s,dict) else s) for s in sol.schemas],
      "roles:",sol.roles,"reuse:",sol.reuse,"build:",sol.build,"viz:",sol.viz_layers,"decider:",sol.decider)
probs=sol.validate(); print("  validate:", probs or "clean")
be=InMemoryBackend(); rep=Deployer(be).deploy_sandbox(sol,"asha-dev")
print("\n[deploy #1] steps:")
for k,r,st in be.log: print(f"   {k:16} {r:60} {st}")
print("  installed:",list(be.installed), "| viz:",sorted(be.viz), "| dev schemas:",list(be.governed_schemas))
print("  marking:", rep["marking"]["badge"]["label"], "| open:", rep["open"][0])
# idempotency: deploy again on SAME backend → everything noop
be.log.clear(); Deployer(be).deploy_sandbox(sol,"asha-dev")
noops=[l for l in be.log if l[2]=="noop"]; news=[l for l in be.log if l[2]=="new"]
print("\n[deploy #2] new=",len(news)," noop=",len(noops))
ok = (not probs and be.installed and "ds:asha-dev:vocab/risk.flood" in be.governed_schemas
      and be.viz=={"risk.flood","risk.exposure"} and rep["marking"]["badge"]["label"]=="Under Development"
      and len(news)==0)
print("\n"+("✅ S1 PROVEN — solution.yaml → whole-solution sandbox deploy; dev-scoped vocab; idempotent; Under Development." if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
