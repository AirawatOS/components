#!/usr/bin/env python3
"""The in-process placement path refuses a component that needs ML libraries (Layer 1 merge, DEBT-B114).

The platform (reference) image carries no ML libraries; they live in the `modeling` runtime. A component that
imports one and declares no runtime profile must be REFUSED in-process with a message naming the runtime — not fail
later with an ImportError inside the run:
  1. an engine that imports xgboost
  2. `from sklearn.model_selection import …` (the from-form)
  3. torch imported in a helper module, not the entrypoint
  4. an indented import inside a function (scipy)
negative controls: a component with only a COMMENTED-OUT ML import, and a plain component, still run in-process.
And the real pm25 engines resolve to the modeling runtime (runtime.profile), so they never take the in-process path.
Exit 1 on failure.
"""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import launcher  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" — " + detail if detail else ""))
    if not ok:
        fails.append(name)


def comp(td, name, files):
    d = os.path.join(td, name)
    os.makedirs(d)
    open(os.path.join(d, "meta.yaml"), "w").write("component_id: ds:test:engine/%s\nkind: engine\nentrypoint: run.py\n" % name)
    for f, src in files.items():
        open(os.path.join(d, f), "w").write(src)
    return d


with tempfile.TemporaryDirectory() as td:
    for name, files, lib in [
        ("uses-xgb", {"run.py": "import xgboost as xgb\nprint('x')\n"}, "xgboost"),
        ("uses-sklearn", {"run.py": "from sklearn.model_selection import GroupKFold\n"}, "sklearn"),
        ("helper-torch", {"run.py": "import helper\n", "helper.py": "import torch\n"}, "torch"),
        ("indented-scipy", {"run.py": "def f():\n    import scipy.stats\n    return 1\nprint(f())\n"}, "scipy"),
    ]:
        rc, out = launcher.place(comp(td, name, files), "tok")
        check("in-process REFUSES %s (imports %s)" % (name, lib), rc == 87 and lib in out and "profile: modeling" in out, out[:120])
    rc, out = launcher.place(comp(td, "commented", {"run.py": "# import xgboost  (not used)\nprint('ok-commented')\n"}), "tok")
    check("a commented-out ML import is NOT refused (runs in-process)", rc == 0 and "ok-commented" in out, "rc=%s" % rc)
    rc, out = launcher.place(comp(td, "plain", {"run.py": "import json\nprint('ok-plain')\n"}), "tok")
    check("a plain component still runs in-process", rc == 0 and "ok-plain" in out, "rc=%s" % rc)

for eng in ("pm25-model", "pm25-apportion"):
    prof = launcher._read_meta(os.path.join(os.path.dirname(HERE), "engines", eng)).get("runtime", {}).get("profile")
    check("engines/%s declares runtime.profile = modeling" % eng, prof == "modeling", "profile=%r" % prof)

print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
sys.exit(1 if fails else 0)
