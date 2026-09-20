"""Governed publish for an ENGINE component through the certify spine — the same steps Solution Studio's
/bridge/project-publish runs, scripted for one engine so it can be re-run from CI or a shell.

  1) POST /v1/certify         → conformance-check the source + sign a certificate (evidence_digest)
  2) POST /v1/store/publish   → list it in the marketplace (version-discipline enforced; needs the cert)
  3) upload source tar → filestore + write a platform.component_asset (SOURCE_ASSET)
     → this is what the app-host / launcher MATERIALISE + digest-verify on load (ADR-0093), so the engine
       is delivered from governance, NOT a baked image.

Run inside a container on the target stack's network. Targets are env-configurable (defaults: airawat-local):
  NET=http://airawat-local-network:8090 KRN=http://airawat-local-kernel:8080
  FS=http://airawat-local-filestore:8095 IDN=http://airawat-local-identity:8093
  DIR=<engine dir>  CID=<component_id>  VERSION=<x.y.z>  PUBLISHER_USER=devrick PUBLISHER_TENANT=airawatdeveloper
"""
import os, io, json, time, tarfile, hashlib, urllib.request as U, urllib.error

NET = os.environ.get("NET", "http://airawat-local-network:8090").rstrip("/")
KRN = os.environ.get("KRN", "http://airawat-local-kernel:8080").rstrip("/")
FS  = os.environ.get("FS",  "http://airawat-local-filestore:8095").rstrip("/")
IDN = os.environ.get("IDN", "http://airawat-local-identity:8093").rstrip("/")
DIR = os.environ.get("DIR", os.path.dirname(os.path.abspath(__file__)))
CID = os.environ.get("CID", "ds:airawatdeveloper:engine/lifecycle-engine")
VERSION = os.environ.get("VERSION", "")
PUB_USER = os.environ.get("PUBLISHER_USER", "devrick")
PUB_TENANT = os.environ.get("PUBLISHER_TENANT", "airawatdeveloper")
SOURCE_ASSET = "__component_source__"


def call(method, url, body=None, token=None, raw=None, ctype="application/json"):
    hdr = {"Content-Type": ctype}
    if token: hdr["Authorization"] = "Bearer " + token
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = U.Request(url, data=data, headers=hdr, method=method)
    try:
        return json.loads(U.urlopen(r, timeout=90).read() or b"{}"), None
    except urllib.error.HTTPError as e:
        return None, f"{e.code}:{e.read().decode()[:200]}"


def read_files(d):
    """Build the conformance MANIFEST: {relpath: sha256hex(content)} plus the raw meta.yaml text. The certify
    check `manifest-bound` requires files['meta.yaml'] == sha256(meta_text), i.e. the file set is a hash
    manifest of the artifact, not the file contents."""
    files, meta_yaml = {}, ""
    for base, _dirs, names in os.walk(d):
        if "__pycache__" in base: continue
        for n in names:
            if n.endswith((".pyc",)): continue
            p = os.path.join(base, n); rel = os.path.relpath(p, d)
            try: raw = open(p, "rb").read()
            except Exception: continue
            files[rel] = hashlib.sha256(raw).hexdigest()
            if rel == "meta.yaml": meta_yaml = raw.decode("utf-8", "replace")
    return files, meta_yaml


def tar_gz(d):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(d, arcname=".")
    return buf.getvalue()


def main():
    files, meta_yaml = read_files(DIR)
    ver = VERSION
    if not ver:
        import re
        m = re.search(r"(?m)^version:\s*(\S+)", meta_yaml); ver = m.group(1) if m else "0.1.0"
    print(f"publishing {CID}@{ver}  from {DIR}  ({len(files)} files)  → {NET}")

    tok = (call("POST", f"{IDN}/login", {"username": PUB_USER, "tenant": PUB_TENANT})[0] or {}).get("access_token")
    if not tok:
        print("FATAL: could not obtain publisher token"); return 1

    # 1) certify (conformance + signed digest)
    cert, e = call("POST", f"{NET}/v1/certify",
                   {"subject_id": CID, "subject_kind": "engine", "meta_yaml": meta_yaml, "files": files}, tok)
    if e:
        print(f"  certify: FAILED {e}")
    else:
        c = cert.get("certificate", cert)
        print(f"  certify: ok  digest={str(c.get('evidence_digest'))[:22]}…  verified={c.get('conformance',{}).get('verified', c.get('verified'))}")

    # 3a) upload source tar → filestore  (do this before publish so we can pass source_file_id)
    blob = tar_gz(DIR)
    fr, e = call("POST", f"{FS}/files", raw=blob, ctype="application/octet-stream")
    fid = (fr or {}).get("id")
    print(f"  filestore: {'ok '+str(fid)[:20] if fid else 'FAILED '+str(e)}")

    # 2) store/publish (marketplace listing; needs the cert)
    pub, e = call("POST", f"{NET}/v1/store/publish",
                  {"component_id": CID, "kind": "engine", "version": ver,
                   "provider": PUB_TENANT, "source_file_id": fid or ""}, tok)
    print(f"  store/publish: {'ok' if not e else 'FAILED '+e}")

    # 3b) component_asset (SOURCE_ASSET) — the record the app-host materialises from (ADR-0093 1a path)
    if fid:
        name = CID.rsplit("/", 1)[-1]; steward = CID.split(":")[1]
        aid = f"ds:{steward}:platform/component_asset/{name}-source"
        rec = {"schema": "platform.component_asset", "id": aid, "tenant": steward, "component": CID,
               "asset_name": SOURCE_ASSET, "file_id": fid, "filename": f"{name}.tar.gz",
               "content_type": "application/gzip", "size_bytes": len(blob),
               "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        r, e = call("POST", f"{KRN}/v1/registry/objects", {"id": aid, "object": rec}, tok)
        print(f"  component_asset: {'ok '+aid.rsplit('/',1)[-1] if not e else 'FAILED '+e}")

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
