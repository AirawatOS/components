# aos — the Airawat build SDK (P1 prototype)

One authoring model for every component kind. Unifies the two runtimes that exist today
(in-process `HANDLERS` engines + headless `main()` drivers) behind a single `ctx` object
and decorators, so the developer writes logic while the SDK owns broker transport, the
`(result, err)` convention, `HANDLERS` assembly, and reads/writes tracking → derived meta.

```python
from aos import engine, ctx

@engine.verb("assess-effectiveness", implements="intelligence.assess_effectiveness@0.1.0")
def assess(body):
    decs = ctx.facts("governance.decision").where(lambda d: d.get("outcome") == "approved")
    ...
    return ctx.write(obj)          # obj | (result, err) | raise aos.Reject(...)

HANDLERS = engine.handlers()       # app_host finds this unchanged
```

## What P1 proves (see the design proposal + harness)
- **Behavioural equivalence** — `components/engines/effectiveness-engine-aos/` is a
  faithful rewrite of the hand-written `effectiveness-engine`; identical output on the
  same inputs (`improved / −88.7% / 7.003→0.79`).
- **Derived meta** — `aos.meta.derive()` produces `implements/verbs/reads/writes/scopes`
  from the code (the decorator + the schemas the SDK saw); `aos.meta.validate()` is the
  single canonical gate (`aos check`), replacing the ≥3 divergent parsers in the tree.
- **No plumbing** — no ctx-dict, no `(result,err)` boilerplate, no manual `HANDLERS`,
  no hand-written `reads`/`writes`/`scopes`.

## Run the equivalence + meta harness
```
python3 <repo>/.../harness_aos.py     # (prototype harness; prints the equivalence + derived meta)
```

## Status / not-yet-P1
- `Ctx.from_broker` (headless transport) is implemented but exercised only lightly;
  the equivalence harness drives the in-process `from_host` path.
- Typed spine sugar (`ctx.recommend/open_case/route_to/decide`) and the `aos` CLI
  (`new/check/test/deploy/publish`) are the next P1/P2 increments.
- Packaging/on-path delivery of `aos` to components at runtime is a P2 concern
  (vendor into the bundle, or put `_common` on the engine load path).
