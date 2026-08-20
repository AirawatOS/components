#!/usr/bin/env python3
"""embed-chunks engine: knowledge.chunk[] -> knowledge.embedding[] via the broker embedding capability.

Confined. It batches the chunk texts through /v1/embed (the model is broker-side + metered, and its NAME
ships back so the stored vector records exactly what produced them), then writes one knowledge.embedding
per chunk keyed by (chunk_hash, model). Idempotent: chunks already embedded with the current model are
skipped. These vectors are the substrate for dense retrieval; brute-force cosine suffices at this scale.
"""
import json, os, urllib.request
API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ["RUN_TOKEN"]
T = os.environ.get("WRITE_TENANT", "kmc")

def _call(path, body, timeout=60):
    # Retry transient broker/filestore errors (5xx / connection); 4xx raise immediately.
    import time as _t
    for _a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Run-Token": TOKEN}, method="POST")
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and _a < 3:
                _t.sleep(1.5 * (_a + 1)); continue
            raise
        except urllib.error.URLError:
            if _a < 3:
                _t.sleep(1.5 * (_a + 1)); continue
            raise

def main():
    chunks = [r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.chunk", "limit": 20000}).get("results", [])]
    if not chunks:
        print("no knowledge.chunk to embed."); return 0
    have = {(e.get("chunk_hash"), e.get("model")) for e in
            (r.get("object") or r for r in _call("/v1/query", {"scope": "knowledge.embedding", "limit": 50000}).get("results", []))}
    have_hashes = {h for (h, _m) in have}
    todo = [c for c in chunks if c.get("chunk_hash") not in have_hashes]      # only embed what isn't embedded yet
    if not todo:
        print(f"all {len(chunks)} chunk(s) already embedded."); return 0
    # Embed in SMALL BATCHES — a single /v1/embed call with hundreds of chunks overwhelms the gateway (a large
    # document has 500+ chunks). Batching keeps each request small and lets a big doc embed reliably.
    BATCH = int(os.environ.get("EMBED_BATCH", "32"))
    model = dim = None; n = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        res = _call("/v1/embed", {"texts": [c["text"] for c in batch], "kind": "doc"}, timeout=120)
        model, dim, vecs = res["model"], res["dim"], res["vectors"]
        for c, vec in zip(batch, vecs):
            if (c["chunk_hash"], model) in have:
                continue
            eid = f"ds:{T}:knowledge/embedding/{c['chunk_hash'].split(':')[1][:16]}-{model}"
            _call("/v1/write", {"scope": "knowledge.embedding", "id": eid, "object": {
                "schema": "knowledge.embedding", "id": eid, "chunk_hash": c["chunk_hash"], "doc_id": c.get("doc_id"),
                "model": model, "dim": dim, "vector": vec, "tenant": T}})
            n += 1
        print(f"  embedded {min(i + BATCH, len(todo))}/{len(todo)} …", flush=True)
    print(f"embedded {n} new chunk(s) with model '{model}' (dim {dim}) in batches of {BATCH}.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
