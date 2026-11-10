import sys; sys.path.insert(0, __import__("os").path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))), "_common"))
from aos.solution import Solution, Deployer, InMemoryBackend

# ADR-0106 S5 — aos destroy (clean teardown) + reproducibility (deploy twice → identical closure).
D = __import__("os").path.join(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))), "solutions", "water-safety-aos")
sol = Solution.load(D)
T = "asha-dev"


def closure(be):
    """The observable closure a deploy stands up — what destroy must remove and a redeploy reproduce."""
    return (frozenset(be.governed_schemas), frozenset(be.governed_roles), frozenset(be.built),
            frozenset(be.installed), frozenset(be.viz), frozenset(be.subs))


be = InMemoryBackend()
d = Deployer(be)

# deploy #1 → snapshot the closure
d.deploy_sandbox(sol, T)
c1 = closure(be)
print("[deploy #1] schemas:", sorted(be.governed_schemas), "| installed:", list(be.installed), "| viz:", sorted(be.viz))
empty = (frozenset(),) * 6

# destroy → closure must be fully torn down
be.log.clear(); rep = d.destroy_sandbox(sol, T)
print("\n[destroy] steps:")
for k, r, st in be.log: print(f"   {k:20} {str(r)[:50]:50} {st}")
after = closure(be)
torn = after == empty
print("  closure after destroy:", "EMPTY ✓" if torn else after)

# destroy again → idempotent: no crash, nothing left, every step 'gone'/'ok'
be.log.clear(); d.destroy_sandbox(sol, T)
idem = closure(be) == empty
print("[destroy #2] idempotent:", idem)

# deploy #2 → closure identical to #1 (reproducible from solution.yaml alone)
d.deploy_sandbox(sol, T)
c2 = closure(be)
repro = c1 == c2
print("\n[deploy #2 after destroy] reproducible:", repro, "| schemas:", sorted(be.governed_schemas))

ok = torn and idem and repro
print("\n" + ("✅ S5 PROVEN — aos destroy tears the closure down; idempotent; deploy→destroy→deploy is reproducible (identical closure)."
              if ok else "❌ inspect above"))
sys.exit(0 if ok else 1)
