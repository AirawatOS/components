"""REGISTER-OCR — import a scanned paper register into a Workspace sheet (Sheets "Import from scan").

For one doc.sheet_import (IMPORT_ID, or the oldest pending one), this engine reads the scan (PDF pages or photos) through
the broker, turns each page upright (PP-LCNet document-orientation model), reads it with the register type's form
knowledge (pf1.py: the printed grid, cell-by-cell PP-OCRv5 reading, candidate readings with domain priors, the ledger's
arithmetic) and writes back into the same record:
  cells      the sheet: values as read (or as corrected), plus the form's rules as formulas (TOTAL computed, closing);
  cell_info  per cell: state (ok | corrected | check | ledger | note), what was read and how sure, the suggestion and the
             rule behind it, and where it came from (page + box on the page image) — for review against the original;
  pages      a reduced image of each upright page (filestore), which the review pane crops;
  summary    blocks found, rules passing before and after correction, corrections, cells to check.
Everything runs in the in-house `paddleocr` runtime with no network: registers hold personal pay data.
Form types: pf1 (P.F.-1 Provident Fund Ledger). Others follow as form definitions.
"""
import base64, io, json, os, re, sys, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

API = os.environ.get("AIRAWAT_API", "http://localhost:8080")
TOKEN = os.environ.get("RUN_TOKEN", "")
T = os.environ.get("WRITE_TENANT") or os.environ.get("TENANT") or ""
SCHEMA = "doc.sheet_import"
PAGE_W = 1600                                   # width of the stored review image of a page
MONTHS = ["April", "May", "June", "July", "August", "September", "October", "November", "December", "January", "February", "March"]


def call(path, body, timeout=120):
    for a in range(4):
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Run-Token": TOKEN})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code >= 500 and a < 3:
                time.sleep(2 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < 3:
                time.sleep(2 * (a + 1)); continue
            raise


def mine(scope, limit=5000):
    rows, cur = [], None
    while len(rows) < limit:
        body = {"scope": scope, "limit": min(1000, limit - len(rows))}
        if cur:
            body["cursor"] = cur
        d = call("/v1/query", body)
        rows += [r.get("object") or r for r in (d.get("results") or [])]
        cur = d.get("next_cursor")
        if not cur:
            break
    return [o for o in rows if str(o.get("id", "")).startswith("ds:%s:" % T)]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write(obj):
    t = now(); obj = {k: v for k, v in obj.items() if v is not None}
    obj["updated_at"] = t
    call("/v1/write", {"scope": SCHEMA, "id": obj["id"], "object": obj, "valid_time": {"from": t}, "asserted_at": t})


def put_file(raw):
    r = call("/v1/file/put", {"content_b64": base64.b64encode(raw).decode()})
    return r.get("id") or r.get("file_id")


# ---- pages ---------------------------------------------------------------------------------------------------------
def pages_of(raw):
    """Upright RGB pages (numpy) from a PDF or an image, rendered at 200 dpi."""
    import numpy as np
    from PIL import Image, ImageOps
    from paddleocr import DocImgOrientationClassification
    if raw[:4] == b"%PDF":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(raw)
        ims = [doc[i].render(scale=200 / 72).to_pil().convert("RGB") for i in range(len(doc))]
    else:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        if max(im.size) < 2200:                     # a phone photo: bring it near a 200 dpi page scan
            k = 2339 / max(im.size); im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
        ims = [im]
    ori = DocImgOrientationClassification(model_name="PP-LCNet_x1_0_doc_ori")
    out = []
    for im in ims:
        small = im.copy(); small.thumbnail((1200, 1200))
        lab = list(ori.predict(np.asarray(small)[:, :, ::-1]))[0]["label_names"][0]   # the turn that makes it upright
        out.append(np.asarray(im.rotate(int(lab), expand=True)) if lab != "0" else np.asarray(im))
    return out


# ---- the sheet -------------------------------------------------------------------------------------------------------
def col_letter(i):
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[i]


def layout_pf1(blocks):
    """Blocks → sheet cells + cell_info. Per subscriber block: a title row, headers, Opening Balance, April–March,
    TOTAL as written, TOTAL computed (formulas), Balance on 31st; one blank row between blocks."""
    import pf1
    cells, info = {}, {}
    COLS = [("dep", "Deposits"), ("con", "Contributions"), ("wd", "Withdrawals"), ("int", "Interest"), ("rem", "Closing balance")]
    r = 1
    cells["A1"] = "P.F.-1 Provident Fund Ledger (imported from scan)"; r = 3
    for n, (b, R) in enumerate(blocks, 1):
        corr = {tuple(c["box"]): c for c in R["corrections"] if c.get("box")}
        rev = {tuple(c["box"]): c for c in R["review"] if c.get("box")}
        cells["A%d" % r] = "Ledger %d — page %d, %s half (name: …)" % (n, b["page"], "left" if b["half"] == 0 else "right")
        r += 1
        cells["A%d" % r] = "Month"
        for k, (_c, h) in enumerate(COLS):
            cells["%s%d" % (col_letter(k + 1), r)] = h
        r += 1
        r_open = r; r_apr = r + 1; r_mar = r + 12; r_tot = r + 13; r_cmp = r + 14; r_b31 = r + 15
        rows = [(r_open, "Opening Balance", b["rows"]["open"])] + [(r_apr + i, MONTHS[i], m) for i, m in enumerate(b["months"])] + \
               [(r_tot, "TOTAL (as written)", b["rows"].get("total", {})), (r_b31, "Balance on 31st March", b["rows"].get("b31", {}))]
        for row, label, src in rows:
            cells["A%d" % row] = label
            for k, (c, _h) in enumerate(COLS):
                cell = src.get(c)
                if not cell:
                    continue
                ref = "%s%d" % (col_letter(k + 1), row)
                box = tuple(cell["box"])
                cand = pf1.cell_cands(cell)
                read_val = cand[0][0] if cand else None
                ci = {"read": cell["read"], "conf": cell["conf"], "page": b["page"], "box": [round(v * b["scale"]) for v in cell["box"]]}
                amount_row = c != "rem" or row in (r_tot, r_b31)        # REMARKS holds an amount only on the closing lines
                if not cell["read"].strip() and not amount_row:
                    continue                                            # stray ink with nothing readable: leave it out
                if not amount_row or pf1.is_note(cell):
                    ci.update(state="note"); cells[ref] = cell["read"]; info[ref] = ci; continue
                if box in corr:
                    x = corr[box]; ci.update(state="corrected", suggest=x["suggest"], rule=x["rule"], how=x.get("how", ""))
                    cells[ref] = str(x["suggest"])
                elif box in rev:
                    x = rev[box]
                    if x.get("how", "").startswith("ledger"):
                        ci.update(state="ledger", suggest=x.get("suggest"), rule=x["rule"], how=x["how"])
                    else:
                        ci.update(state="check", suggest=x.get("suggest"), rule=x["rule"], how=x.get("how", "not confident"))
                    v = read_val if read_val is not None else x.get("suggest")
                    cells[ref] = "" if v is None else str(v)
                else:
                    ci.update(state="ok" if read_val is not None else "check")
                    if read_val is None:
                        ci.update(rule="no amount could be read here", how="unreadable")
                    cells[ref] = "" if read_val is None else str(read_val)
                info[ref] = ci
        # the form's rules, as formulas the user can see
        cells["A%d" % r_cmp] = "TOTAL (computed)"
        cells["B%d" % r_cmp] = "=B%d+SUM(B%d:B%d)" % (r_open, r_apr, r_mar)
        for k in (2, 3, 4):
            L = col_letter(k)
            cells["%s%d" % (L, r_cmp)] = "=SUM(%s%d:%s%d)" % (L, r_apr, L, r_mar)
        cells["F%d" % r_cmp] = "=B%d+C%d+E%d-D%d" % (r_tot, r_tot, r_tot, r_tot)
        r = r_b31 + 2
    return cells, info, r, len(COLS) + 1


# ---- the run ---------------------------------------------------------------------------------------------------------
def run_import(rec):
    import cv2
    import pf1
    rec = dict(rec, status="running", started_at=now(), engine="register-ocr", error=None)
    write(rec)
    form = rec.get("form") or "pf1"
    if form != "pf1":
        raise ValueError("register type %r is not supported yet (supported: pf1)" % form)
    blocks, pages, n = [], [], 0
    for sf in rec.get("source_files") or []:
        raw = base64.b64decode((call("/v1/file/get", {"id": sf["file_id"]}, timeout=120) or {}).get("content_b64") or "")
        for img in pages_of(raw):
            n += 1; t0 = time.time()
            h, w = img.shape[:2]; scale = min(1.0, PAGE_W / w)
            small = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", cv2.cvtColor(small, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 82])
            pages.append({"page": n, "file_id": put_file(jpg.tobytes()), "width": small.shape[1], "height": small.shape[0],
                          "source": sf.get("name") or sf["file_id"][:20]})
            found = 0
            for b in pf1.page(img):
                if "error" in b:
                    continue
                b["page"] = n; b["scale"] = scale
                blocks.append((b, pf1.check_block(b))); found += 1
            print("page %d: %d ledger blocks in %.1f s" % (n, found, time.time() - t0), flush=True)
    if not blocks:
        raise ValueError("no P.F.-1 ledger grid was found on the %d page(s) — is the page flat, whole and in focus?" % n)
    cells, info, rows, cols = layout_pf1(blocks)
    rules = [v for _b, R in blocks for v in R["rules"].values()]
    states = [ci["state"] for ci in info.values()]
    summary = {"pages": n, "blocks": len(blocks), "rules": len(rules), "rules_ok_as_read": sum(v["raw"] for v in rules),
               "rules_ok_after": sum(v["after"] for v in rules), "cells": len(info),
               **{s: states.count(s) for s in ("ok", "corrected", "check", "ledger", "note")}}
    write(dict(rec, status="done", finished_at=now(), pages=pages, cells=cells, cell_info=info, summary=summary))
    print("RESULT: " + json.dumps({"import": rec["id"], **summary}))


def main():
    if not T:
        raise SystemExit("FATAL: no tenant for this run (WRITE_TENANT / TENANT)")
    want = os.environ.get("IMPORT_ID")
    recs = [r for r in mine(SCHEMA) if (r["id"] == want if want else r.get("status") == "pending")]
    recs.sort(key=lambda r: str(r.get("requested_at") or r.get("created_at") or ""))
    if not recs:
        print("no %s to process%s" % (SCHEMA, " (IMPORT_ID=%s)" % want if want else "")); return 0
    rec = recs[0]
    try:
        run_import(rec)
    except Exception as e:
        write(dict(rec, status="failed", finished_at=now(), error="%s: %s" % (type(e).__name__, str(e)[:300])))
        print("FAILED: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
