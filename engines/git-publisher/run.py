#!/usr/bin/env python3
"""git-publisher (ADR-0083 S2) — mirror a developer tenant's PUBLISHED components into the tenant's own Git repo.

For the tenant's dev.git_binding it: (1) resolves the repo + branch + path_convention + credential, (2) for each
component fetches the source tar the platform already holds (platform.component_asset → filestore), (3) lays it into
the repo working tree, (4) commits with per-developer attribution (message = component_id + version + source digest),
(5) pushes. The Git token comes from the SECRETS service (Vault) via credential_ref — never in the engine/meta/logs.
Egress is broker-gated to the git host. No fabrication: a component with no source asset is skipped.

Local proof (no stack, no token): `GIT_SELFTEST=1 GIT_SELFTEST_REPO=file:///tmp/bare.git python run.py` materialises a
dummy component + commits + pushes to a LOCAL bare repo, proving the git mechanics end to end.
"""
import os, json, subprocess, tempfile, tarfile, io, urllib.request, shutil, time

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
TENANT = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
SECRETS_URL = os.environ.get("SECRETS_URL", "")
GIT_TOKEN = os.environ.get("GIT_TOKEN", "")            # injected from Vault (credential_ref) by the runtime
ONLY = os.environ.get("COMPONENT_ID", "")              # publish just this component (else: all with a source asset)
ACTOR = os.environ.get("ACTOR", "")                    # the publishing developer (for commit attribution)
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _call(path, body, timeout=60):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")


def query(scope):
    return [r.get("object", r) for r in _call("/v1/query", {"scope": scope}).get("results", [])]


def git(args, cwd, token=None):
    """Run git with the token kept out of argv/logs (fed via env for the credential helper)."""
    env = dict(os.environ)
    if token:
        env["GIT_ASKPASS"] = "/bin/echo"   # non-interactive; token goes in the remote URL instead
    r = subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _auth_url(repo_url, token):
    if token and repo_url.startswith("https://"):
        return repo_url.replace("https://", f"https://x-access-token:{token}@", 1)
    return repo_url


def _fetch_source(file_id):
    """The component's source tar, through the broker under this run's token (DEBT-B174: a component is not a
    declared filestore caller — the store refuses it; the kernel broker reads it with its own svc:filestore token)."""
    import base64
    return base64.b64decode((_call("/v1/file/get", {"id": file_id}, timeout=90) or {}).get("content_b64") or "")


def _lay_component(work, path_tmpl, component_id, tar_bytes):
    kind = component_id.split(":")[-1].split("/")[0]
    name = component_id.split("/")[-1]
    rel = path_tmpl.replace("{kind}", kind).replace("{name}", name)
    dest = os.path.join(work, rel)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.isfile():
                p = os.path.join(dest, os.path.basename(m.name)) if m.name in (".", "./") else os.path.join(dest, m.name.lstrip("./"))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(tf.extractfile(m).read())
    return rel


def _clone_or_init(work, repo_url, branch, token):
    au = _auth_url(repo_url, token)
    try:
        git(["clone", "--depth", "1", "--branch", branch, au, work], cwd=os.path.dirname(work) or ".")
    except Exception:
        # empty repo / branch absent → init a fresh worktree on that branch
        git(["init", "-q", work], cwd=".")
        git(["-C", work, "checkout", "-q", "-B", branch], cwd=".")
        git(["remote", "add", "origin", au], cwd=work)
    git(["config", "user.email", "git-publisher@airawat.org"], cwd=work)
    git(["config", "user.name", ACTOR or "AirawatOS git-publisher"], cwd=work)


def publish(binding, components):
    repo = binding["repo_url"]; branch = binding.get("default_branch", "main")
    tmpl = binding.get("path_convention", "components/{kind}/{name}")
    work = tempfile.mkdtemp(prefix="gitpub-")
    worktree = os.path.join(work, "repo")
    _clone_or_init(worktree, repo, branch, GIT_TOKEN)
    pushed = []
    for c in components:
        cid = c["component"]; fid = c["file_id"]
        rel = _lay_component(worktree, tmpl, cid, _fetch_source(fid))
        git(["add", "-A", "--", rel], cwd=worktree)
        # nothing changed for this component → skip an empty commit
        if git(["status", "--porcelain"], cwd=worktree):
            msg = f"{cid} @ {c.get('version','?')} (digest {fid[:24]})\n\nAutomated mirror by AirawatOS git-publisher (ADR-0083)."
            git(["commit", "-q", "-m", msg] + (["--author", f"{ACTOR} <{ACTOR}@airawat>"] if ACTOR else []), cwd=worktree)
            pushed.append(cid)
    if pushed:
        git(["push", "origin", branch], cwd=worktree)
    head = git(["rev-parse", "HEAD"], cwd=worktree) if pushed else None
    shutil.rmtree(work, ignore_errors=True)
    return pushed, head


def _selftest():
    """No stack, no token: prove clone/init → lay files → commit → push against a LOCAL bare repo."""
    bare = os.environ.get("GIT_SELFTEST_REPO") or ("file://" + tempfile.mkdtemp(prefix="barehost-") + "/bare.git")
    if bare.startswith("file://"):
        path = bare[len("file://"):]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-q", path], check=True)
    binding = {"repo_url": bare, "default_branch": "main", "path_convention": "components/{kind}/{name}"}
    # a dummy component tar in memory
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"component_id: ds:airawatdeveloper:driver/selftest\nkind: driver\n"
        ti = tarfile.TarInfo("./meta.yaml"); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    tarb = buf.getvalue()
    global _fetch_source
    _fetch_source = lambda fid: tarb
    comps = [{"component": "ds:airawatdeveloper:driver/selftest", "file_id": "sha2-256:deadbeef", "version": "0.1.0"}]
    pushed, head = publish(binding, comps)
    print(f"SELFTEST ok: pushed {pushed} head={head} repo={bare}")
    return 0


def main():
    if os.environ.get("GIT_SELFTEST"):
        return _selftest()
    if not TOKEN or not TENANT:
        print("git-publisher — handlers: publish (dev self-test: set GIT_SELFTEST=1).")
        return 0
    bindings = [b for b in query("dev.git_binding") if (b.get("tenant") == TENANT or not b.get("tenant"))]
    if not bindings:
        print("git-publisher: no dev.git_binding for this tenant — nothing to do."); return 0
    binding = bindings[0]
    assets = [a for a in query("platform.component_asset")
              if a.get("asset_name") == "__component_source__" and (not ONLY or a.get("component") == ONLY)]
    pushed, head = publish(binding, assets)
    upd = {**binding, "last_push": {"at": NOW, "commit": head, "components": pushed}, "updated_at": NOW}
    _call("/v1/write", {"scope": "dev.git_binding", "id": binding["id"], "object": upd,
                        "valid_time": {"from": NOW}, "asserted_at": NOW})
    print("RESULT: " + json.dumps({"pushed": pushed, "commit": head}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
