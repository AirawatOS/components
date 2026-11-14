#!/usr/bin/env python3
"""Publish components to the LEGACY STAGING stack (EC2, pre-Layer-1 images) as the developer (airawatdeveloper) — the
Studio path: re-home the id to the developer, CERTIFY (meta + file hashes), deliver the source (filestore +
platform.component_asset __component_source__), then try store/publish WITH source_file_id.

A development-tenant component is refused by store/publish (ADR-0106); promote it with a publish REQUEST that a
standards-body steward approves (POST /v1/store/publish-requests as the developer, then …/publish-requests/decide).
After publishing a new version of an IN-PROCESS engine, the app-host keeps its cached copy until that cache folder
(/data/app-bundles/.components-cache/<id>) is removed and app-host restarted.

Usage (from a components checkout, credential kept in a private file on the host, never on the command line or printed):
    tar czf - -C engines <name>… | ssh <staging> 'D=$(mktemp -d); tar xzf - -C $D; docker cp $D airawat-app-host:/tmp/pub'
    ssh <staging> 'docker exec -i -e SRC=/tmp/pub -e DEVPW="$(cat ~/.aos-dev-cred)" airawat-app-host python3 - <name>…' < tools/publish_to_staging.py
This is an operator tool for the legacy staging stack: it addresses that stack's filestore directly (its filestore has no
service-token gate). The current platform delivers components through its own installer / Studio path instead."""
import hashlib, io, json, os, sys, tarfile, time, urllib.error, urllib.request

IDN, NET, KRN, FS = "http://identity:8093", "http://network:8090", "http://kernel:8080", "http://filestore:8095"
DEV_REALM, DEV_USER = "airawatdeveloper", "devrick"


def post(url, body, tok=None, ctype="application/json", timeout=180):
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    h = {"Content-Type": ctype}
    if tok:
        h["Authorization"] = "Bearer " + tok
    try:
        return 200, json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method="POST"), timeout=timeout).read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")[:600].decode(errors="replace")


s, d = post(IDN + "/realms/%s/login" % DEV_REALM, {"username": DEV_USER, "password": os.environ["DEVPW"]})
if s != 200:
    sys.exit("developer login refused: %s %s" % (s, str(d)[:120]))
TOK = d["access_token"]
print("developer signed in: %s@%s" % (DEV_USER, DEV_REALM))


def meta_fields(text):
    m = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].rstrip()
        if line and line[0] != " " and ":" in line:
            k, v = line.split(":", 1)
            m[k.strip()] = v.strip()
    return m


def lst(v):
    v = (v or "").strip()
    return [x.strip() for x in v[1:-1].replace("\n", " ").split(",") if x.strip()] if v.startswith("[") else []


def full_list(text, key):
    """reads/writes may span lines ([a, b,\n  c]) — read the bracketed list across lines."""
    i = text.find("\n" + key + ":")
    if i < 0:
        return []
    j = text.find("[", i); k = text.find("]", j)
    return [x.strip() for x in text[j + 1:k].replace("\n", " ").split(",") if x.strip()]


for name in sys.argv[1:]:
    d = os.path.join(os.environ["SRC"], name)
    meta_path = os.path.join(d, "meta.yaml")
    text = open(meta_path).read()
    old = meta_fields(text)["component_id"]
    cid = "ds:%s:%s" % (DEV_REALM, old.split(":", 2)[2])
    text = text.replace("component_id: " + old, "component_id: " + cid, 1)
    open(meta_path, "w").write(text)
    m = meta_fields(text)
    ver = m["version"].split()[0]
    files = {}
    for base, _dirs, names in os.walk(d):
        if "__pycache__" in base:
            continue
        for n in names:
            if n.endswith((".pyc", ".log")):
                continue
            p = os.path.join(base, n)
            files[os.path.relpath(p, d)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    print("\n== %s %s (%d files)" % (cid, ver, len(files)))
    s, cert = post(NET + "/v1/certify", {"subject_id": cid, "subject_kind": "component", "meta_yaml": text, "files": files}, TOK)
    status = (cert.get("certificate") or cert).get("status") if isinstance(cert, dict) else None
    print("  certify:", s, status if status else str(cert)[:400])
    if s != 200 or status not in ("valid", None):
        print("  STOP: not certified"); continue
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(d, arcname=".", filter=lambda ti: None if "__pycache__" in ti.name else ti)
    blob = buf.getvalue()
    s, fr = post(FS + "/files", blob, None, ctype="application/octet-stream")
    fid = fr.get("id") if isinstance(fr, dict) else None
    print("  source → filestore:", s, fid, "(%d bytes)" % len(blob))
    if not fid:
        print("  STOP: source not stored"); continue
    steward, short = cid.split(":")[1], cid.rsplit("/", 1)[-1]
    aid = "ds:%s:platform/component_asset/%s-source" % (steward, short)
    rec = {"schema": "platform.component_asset", "id": aid, "tenant": steward, "component": cid,
           "asset_name": "__component_source__", "file_id": fid, "filename": short + ".tar.gz",
           "content_type": "application/gzip", "size_bytes": len(blob), "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    s, r = post(KRN + "/v1/registry/objects", {"id": aid, "object": rec}, TOK)
    print("  source asset record:", s, aid if s == 200 else str(r)[:200])
    writes, reads = full_list(text, "writes"), full_list(text, "reads")
    scopes = [{"verb": "write", "scope": x} for x in writes] + [{"verb": "read", "scope": x} for x in reads]
    body = {"component_id": cid, "kind": m["kind"], "version": ver, "provider": "ds:%s:developer" % DEV_REALM,
            "scopes": scopes, "runtime_profile": "", "source_file_id": fid,
            "title": m.get("display_name") or short, "description": "published from components feat/city-profile-ask"}
    s, r = post(NET + "/v1/store/publish", body, TOK)
    print("  store/publish:", s, (r.get("version") if isinstance(r, dict) else str(r)[:300]))
