"""Prototype: domain-aware reading of a P.F.-1 Provident Fund Ledger page (two ledgers side by side, two subscriber
blocks each). Steps: find the printed grid → cut cells → read each cell with the PP-OCRv5 recogniser (upright and turned
180°) → keep several candidate readings per amount cell (digit-only, OCR letter→digit variants, the column's usual
value) → choose the readings that satisfy the ledger's arithmetic → report corrections and cells for a person to check.

usage: python3 pf1.py <pdf> <out.json>        (runs inside the paddleocr runtime; no network)
"""
import itertools, json, re, sys, time
import cv2, numpy as np, pypdfium2 as pdfium
from paddleocr import TextRecognition

REC = TextRecognition(model_name="PP-OCRv5_server_rec")
MONTHS = ["apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "jan", "feb", "mar"]


import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from grid import masks, grid_half, anchor_rows, const_track, cells_from, x_at


# ---- reading -----------------------------------------------------------------------------------------------------
def rec(imgs):
    if not imgs: return []
    out = []
    for r in REC.predict(input=[cv2.cvtColor(i, cv2.COLOR_GRAY2BGR) for i in imgs], batch_size=16):
        out.append((str(r["rec_text"]), float(r["rec_score"])))
    return out


FIX = {"o": "0", "O": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1", "i": "1", "/": "1", "S": "5", "s": "5",
       "B": "8", "Z": "2", "z": "2", "g": "9", "q": "9", "b": "6", "G": "6", "T": "7", "A": "4", "h": "4"}
DOUBLE = set("wmnuvωW")         # a handwritten "00" (or "0") that OCR reads as one letter


def candidates(text, score):
    """Digit readings of one OCR string, best first: as read; with letter→digit fixes; each wide letter as 0 or 00."""
    t = re.sub(r"[\s,]", "", text).strip("-—–~=<>.✓→")
    if not t or not re.search(r"[0-9]", t) and len(t) > 3: return []
    if re.search(r"[A-Za-z]{3,}", t) or "(" in t: return []           # words or a bracketed note, not an amount
    base = "".join(FIX.get(ch, ch) for ch in t)
    slots = [i for i, ch in enumerate(base) if ch in DOUBLE]
    out = []
    for combo in itertools.product(("0", "00"), repeat=min(len(slots), 3)):
        s = list(base)
        for i, rep in zip(slots, combo): s[i] = rep
        s = "".join(s)
        if re.fullmatch(r"\d{1,9}", s):
            pen = (0 if base == t else 0.05) + 0.05 * sum(r == "00" for r in combo)
            out.append((int(s), round(score - pen, 3)))
    return out


# ---- one page ----------------------------------------------------------------------------------------------------
ORDER = ["dep", "con", "total", "wd", "actual", "int", "loss", "rem"]
HEAD = {"depos": "dep", "contri": "con", "withdr": "wd", "remark": "rem", "year": "label"}


def raw_strips(cols, H, x0, x1):
    """The found strips, plus a strip each side in case an outer border (label or REMARKS) was too faint to find."""
    w = np.median([x_at(c["right"], H / 2) - x_at(c["left"], H / 2) for c in cols])
    st = []
    xl = x_at(cols[0]["left"], H / 2)
    if xl - x0 > 60:
        st.append((const_track(max(x0, xl - 260), H), cols[0]["left"], cols[0]["lines"]))
    st += [(c["left"], c["right"], c["lines"]) for c in cols]
    xr = x_at(cols[-1]["right"], H / 2)
    if x1 - xr > 60:
        st.append((cols[-1]["right"], const_track(min(x1 - 2, xr + 1.8 * w), H), cols[-1]["lines"]))
    return [("s%d" % i,) + t for i, t in enumerate(st)]


def month_hits(texts):
    return sum(any(re.sub(r"[^a-z]", "", t.lower()).startswith(m) for m in MONTHS + ["open", "tot"]) for t in texts)


def page(img_rgb):
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    hor, ver, ink = masks(gray)
    H, W = gray.shape
    blocks = []
    for half, (x0, x1) in enumerate(((0, W // 2), (W // 2, W))):
        cols = grid_half(hor, ver, x0, x1, H)
        if len(cols) < 6:
            blocks.append({"half": half, "error": "grid not found (%d columns)" % len(cols)}); continue
        L, rws = anchor_rows(cols)
        strips = raw_strips(cols, H, x0, x1)
        allc = cells_from(strips, None, L, rws)
        cells, crops = [], []
        for c in allc:
            xa, ya, xb, yb = c["box"]; xa += 3; xb -= 3; ya += 2; yb -= 2
            if xb - xa < 15 or yb - ya < 10: continue
            inkf = ink[ya:yb, xa:xb].mean() / 255.0
            if inkf < 0.012: continue                                 # empty cell
            c["ink"] = round(inkf, 3); cells.append(c)
            crops.append(gray[max(0, ya - 4):yb + 4, xa:(xb if c["col"] == "label" else min(gray.shape[1], xb + 28))])
        up = rec(crops); down = rec([cv2.rotate(c, cv2.ROTATE_180) for c in crops])
        for cell, u, d in zip(cells, up, down):
            cell["read"] = u[0]; cell["conf"] = round(u[1], 3); cell["read_180"] = d[0]; cell["conf_180"] = round(d[1], 3)
        # the label column is the strip where month names are written; the columns after it follow the printed order
        by = {}
        for cell in cells: by.setdefault(cell["col"], []).append(cell["read"])
        names = [n for n, *_ in strips]
        li = max(range(len(names)), key=lambda i: month_hits(by.get(names[i], [])))
        rename = {names[li]: "label"}; rename.update({n: ORDER[k] for k, n in enumerate(names[li + 1:li + 1 + len(ORDER)])})
        cells = [dict(c, col=rename[c["col"]]) for c in cells if c["col"] in rename]
        heads = ["%s=%s" % (k, v) for k, v in rename.items()]
        rows = {}
        for cell in cells:
            rows.setdefault(cell["row"], {})[cell["col"]] = cell
        labelled = []
        for r in sorted(rows):
            lab = re.sub(r"[^a-z]", "", (rows[r].get("label") or {}).get("read", "").lower())
            kind = ("open" if lab.startswith("open") or "balance" in lab and "31" not in (rows[r].get("label") or {}).get("read", "") and lab.startswith("op")
                    else "total" if lab.startswith("tot") or lab.endswith("otal") and len(lab) <= 6 else next((m for m in MONTHS if lab.startswith(m)), None))
            labelled.append((r, kind))
        # the form's fixed shape: Opening Balance, April … March, TOTAL, Balance on 31st. Every label read votes for
        # where its block's April row is; misread labels are filled in from the shape.
        votes = {}
        for r, kind in labelled:
            a = r + 1 if kind == "open" else r - 12 if kind == "total" else r - MONTHS.index(kind) if kind in MONTHS else None
            if a is not None: votes[a] = votes.get(a, 0) + (2 if kind in ("open", "total") else 1)
        aprils = sorted(a for a, v in votes.items() if v >= 2)
        kept = []
        for a in sorted(aprils, key=lambda a: -votes[a]):
            if all(abs(a - k) > 13 for k in kept): kept.append(a)
        for a in sorted(kept):
            g = lambda r: rows.get(r, {})
            blocks.append({"half": half, "heads": heads, "votes": votes[a], "rows": {"open": g(a - 1), "total": g(a + 12), "b31": g(a + 13)},
                           "months": [g(a + k) for k in range(12)]})
    return blocks


# ---- the ledger's arithmetic ---------------------------------------------------------------------------------------
def cell_cands(cell, extras=()):
    """Candidate values for one cell, best first: the upright reading (digit-normalised), the 180° reading, then
    domain priors (`extras`: the neighbouring months' amounts, the column's usual amount) at a lower score, so the
    arithmetic can choose them only when the readings do not add up. A confident reading keeps its weight."""
    if not cell: return [(0, 1.0)]                                       # an empty cell is zero
    c = candidates(cell["read"], cell["conf"]) + [(v, s - 0.3) for v, s in candidates(cell["read_180"], cell["conf_180"])]
    for v, sc in extras:
        if v is not None and c and all(v != x for x, _ in c):
            c.append((v, sc * (1.0 - min(cell["conf"], 0.99))))           # a sure reading is hard to overrule
    best = {}
    for v, s in c:
        best[v] = max(best.get(v, -1), s)
    return sorted(best.items(), key=lambda x: -x[1])


def lev(a, b):
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (ca != cb))
    return d[-1]


def solve_sum(parts, total, reads=None, confs=None):
    """parts: [candidate lists] that must add up to one of `total`'s candidates. Changes as few cells as possible
    (0, 1 or 2 away from the first reading), best score first. Returns (values, total, changed indices) or None."""
    first = [p[0][0] if p else None for p in parts]
    tvals = dict(total) if total else {}
    best = None
    for k in range(0, 3):
        for idx in itertools.combinations(range(len(parts)), k):
            pools = [parts[i][1:] if i in idx else parts[i][:1] for i in range(len(parts))]
            if any(not p for p in pools): continue
            for combo in itertools.product(*pools):
                s = sum(v for v, _ in combo)
                if s in tvals:
                    sc = sum(x for _, x in combo) + tvals[s]
                    if best is None or sc > best[0]: best = (sc, [v for v, _ in combo], s, list(idx))
        if best: return best[1:] + ("sum",)
    # one cell misread (a digit dropped, added or changed): the total gives its value, accepted only if that value is
    # within two digit edits of what was read (or the cell was not read at all)
    if total and reads and all(parts):
        tops = [p[0][0] for p in parts]; best = None
        for tv, ts in total[:2]:
            for i in range(len(parts)):
                v = tv - (sum(tops) - tops[i])
                if v < 0 or v == tops[i]: continue
                rd = re.sub(r"\D", "", "".join(FIX.get(ch, ch) for ch in reads[i] or ""))
                dist = lev(str(v), rd) if rd else 0
                dropped = len(rd) < len(str(v)) and dist == len(str(v)) - len(rd)       # digits lost, the rest kept
                unsure = confs is not None and confs[i] < 0.85 and dist == 1
                if (dropped or unsure) and dist <= 2 and (best is None or dist < best[0]):
                    vals = list(tops); vals[i] = v; best = (dist, vals, tv, [i])
        if best: return best[1:] + ("digit repaired",)
    unread = [i for i, p in enumerate(parts) if not p]                    # one unreadable cell: the total gives it
    if len(unread) == 1 and total:
        vals = [p[0][0] if p else None for p in parts]
        t = total[0][0]; rest = sum(v for v in vals if v is not None)
        if t >= rest:
            vals[unread[0]] = t - rest; return vals, t, unread, "inferred"
    return None


AMOUNT_COLS = ("dep", "con", "wd", "int")
NAMES = {"dep": "opening + monthly deposits = TOTAL", "con": "contributions = TOTAL", "wd": "withdrawals = TOTAL", "int": "interest = TOTAL"}


def is_note(cell):
    """A clerk's note in an amount column (a date, "GPF adv", a tick), not an amount: no digits, a bracket, or at
    most two digits written small. Amounts in this ledger are three digits or more."""
    t = cell["read"]
    digits = re.sub(r"\D", "", "".join(FIX.get(ch, ch) for ch in t))
    return not digits or "(" in t or len(digits) <= 2


def parts_for(col, cells):
    """Candidate lists for a column's cells, with the domain priors (neighbouring months, the usual amount)."""
    first = []
    for c in cells:
        cc = cell_cands(c) if c and not is_note(c) else []
        first.append(cc[0][0] if cc else None)
    seen = [v for v in first if v]
    mode = max(set(seen), key=seen.count) if len(seen) >= 4 and max(map(seen.count, set(seen))) >= 3 else None
    off = 1 if col == "dep" else 0
    parts = []
    for k, c in enumerate(cells):
        if not c or is_note(c): parts.append([(0, 1.0)]); continue
        ex = []
        if k >= off:
            for nb in (k - 1, k + 1):
                if off <= nb < len(cells) and first[nb]: ex.append((first[nb], 0.8))
            if mode: ex.append((mode, 0.6))
        parts.append(cell_cands(c, ex))
    return parts


def solve_col(parts, cells, tc, tcell):
    """→ (values, total, [corrections]) or None. Tries: the readings as they are; then 1–2 cells changed to their
    other candidates; then one cell or the TOTAL itself repaired (within two digit edits of what was read)."""
    reads = [c["read"] if c else "" for c in cells]
    confs = [c["conf"] if c else 1.0 for c in cells]
    sol = solve_sum(parts, tc, reads, confs) if tc else None
    if not sol and all(parts):
        S = sum(p[0][0] for p in parts)                                   # the TOTAL misread (often a digit cut off)
        if tcell and not is_note(tcell):
            rd = re.sub(r"\D", "", "".join(FIX.get(ch, ch) for ch in tcell["read"]))
            lost = len(rd) < len(str(S)) and lev(str(S), rd) == len(str(S)) - len(rd)   # TOTAL ran past the border
            if rd and (lost or tcell["conf"] < 0.7 and lev(str(S), rd) <= 1): sol = ([p[0][0] for p in parts], S, [], "TOTAL repaired")
    if not sol: return None
    vals, t, changed, how = sol
    corr = [{"box": cells[i]["box"] if cells[i] else None, "read": reads[i], "suggest": vals[i], "how": how} for i in changed]
    if tcell and (not tc or tc[0][0] != t):
        corr.append({"box": tcell["box"], "read": tcell["read"], "suggest": t, "what": "TOTAL", "how": how})
    return vals, t, corr, how


def check_block(b):
    R = {"rules": {}, "corrections": [], "review": [], "notes": 0}
    months = b["months"]; tot = b["rows"].get("total", {}); op = b["rows"]["open"]; b31 = b["rows"].get("b31", {})
    cols = {}
    for col in AMOUNT_COLS:
        cells = ([op.get(col)] if col == "dep" else []) + [m.get(col) for m in months]
        tcell = tot.get(col)
        if not any(c and not is_note(c) for c in cells) and not (tcell and not is_note(tcell)):
            R["notes"] += sum(1 for c in cells if c); continue
        R["notes"] += sum(1 for c in cells if c and is_note(c))
        parts = parts_for(col, cells)
        tc = cell_cands(tcell) if tcell and not is_note(tcell) else [(0, 1.0)]
        raw = bool(tc) and all(p for p in parts) and sum(p[0][0] for p in parts) == tc[0][0]
        cols[col] = {"cells": cells, "tcell": tcell, "parts": parts, "tc": tc, "raw": raw, "sol": solve_col(parts, cells, tc, tcell)}
    # the closing balance is written twice (TOTAL line, Balance on 31st): two readings of one number
    cc = cell_cands(tot["rem"]) if tot.get("rem") else []
    bc = cell_cands(b31["rem"]) if b31.get("rem") else []
    closing = None
    if cc and bc:
        a, c2 = cc[0][0], bc[0][0]
        if a == c2: closing = a
        elif lev(str(a), str(c2)) <= 2: closing = max((a, c2), key=lambda v: len(str(v)))   # one copy lost a digit
    raw_t = {k: (v["tc"][0][0] if v["tc"] else 0) for k, v in cols.items()}
    eq = lambda t: t.get("dep", 0) + t.get("con", 0) + t.get("int", 0) - t.get("wd", 0)
    sol_t = {k: v["sol"][1] for k, v in cols.items() if v["sol"]}
    unresolved = [k for k in cols if not cols[k]["sol"]]
    want = eq(sol_t) if not unresolved else None
    # closing written twice and agreeing, but the columns do not reach it: find the ONE column whose total, set to what
    # the closing implies, makes that column add up too (prefer a column that did not add up by itself)
    if closing is not None and (want is None or abs(want - closing) > 1):
        for col in sorted(cols, key=lambda k: (k not in unresolved, k != "dep")):
            others = {k: v for k, v in sol_t.items() if k != col}
            if any(k not in others for k in cols if k != col): continue
            implied = closing - eq(others) if col != "wd" else eq(others) - closing
            if implied < 0: continue
            v = cols[col]
            s2 = solve_col(v["parts"], v["cells"], [(implied, 1.0), (implied + 1, 0.9), (implied - 1, 0.9)], v["tcell"])
            if s2 and s2[3] == "inferred": s2 = None                        # two steps of inference: leave it to a person
            if s2:
                v["sol"] = s2; v["via"] = "closing balance"; sol_t[col] = s2[1]
                unresolved = [k for k in cols if not cols[k]["sol"]]; want = eq(sol_t) if not unresolved else None
                break
    for col, v in cols.items():
        R["rules"][NAMES[col]] = {"raw": bool(v["raw"]), "after": bool(v["sol"])}
        if v.get("via"): R["rules"][NAMES[col]]["via"] = v["via"]
        if v["sol"]:
            R["rules"][NAMES[col]]["how"] = v["sol"][3]
            confid = {tuple(c["box"]): c["conf"] for c in v["cells"] + [v["tcell"]] if c}
            for c in v["sol"][2]:
                sure = confid.get(tuple(c["box"] or ()), 0) >= 0.9 and c["how"] == "sum" and c.get("what") != "TOTAL"
                if sure:      # the sum needs a clearly written amount changed: more likely the clerk's arithmetic
                    R["rules"][NAMES[col]]["after"] = False; R["rules"][NAMES[col]]["ledger_discrepancy"] = True
                    R["review"].append(dict(c, rule=NAMES[col], how="ledger arithmetic? the reading looks clear"))
                else:
                    (R["review"] if c["how"] == "inferred" else R["corrections"]).append(dict(c, rule=NAMES[col]))
        else:
            for c in v["cells"] + [v["tcell"]]:
                if c and not is_note(c) and c["conf"] < 0.9: R["review"].append({"rule": NAMES[col], "box": c["box"], "read": c["read"]})
    if cc or bc:
        R["rules"]["closing = deposits + contributions + interest − withdrawals"] = {
            "raw": bool(cc) and abs(cc[0][0] - eq(raw_t)) <= 1, "after": want is not None and any(abs(x - want) <= 1 for x, _ in cc + bc)}
        R["rules"]["TOTAL-line balance = Balance on 31st"] = {"raw": bool(cc and bc) and cc[0][0] == bc[0][0],
                                                             "after": want is not None and bool(cc and bc)}
        if want is not None:                                              # only when every column adds up
            target = closing if closing is not None and abs(closing - want) <= 1 else want
            for nm, cl, cell in (("TOTAL line", cc, tot.get("rem")), ("Balance on 31st", bc, b31.get("rem"))):
                if cell and (not cl or abs(cl[0][0] - want) > 1):
                    rd = re.sub(r"\D", "", cell["read"])
                    if rd and lev(str(target), rd) > 2:
                        R["rules"]["closing = deposits + contributions + interest − withdrawals"]["after"] = False
                        R["review"].append({"rule": "closing balance (" + nm + ")", "box": cell["box"], "read": cell["read"]}); continue
                    R["corrections"].append({"rule": "closing balance (" + nm + ")", "box": cell["box"], "read": cell["read"], "suggest": target})
        else:
            for nm, cell in (("TOTAL line", tot.get("rem")), ("Balance on 31st", b31.get("rem"))):
                if cell: R["review"].append({"rule": "closing balance (" + nm + ")", "box": cell["box"], "read": cell["read"]})
    return R


def main():
    pdf, out = sys.argv[1], sys.argv[2]
    doc = pdfium.PdfDocument(pdf); res = []
    for i in range(len(doc)):
        t0 = time.time()
        img = np.asarray(doc[i].render(scale=200 / 72).to_pil().convert("RGB").rotate(90, expand=True))
        for b in page(img):
            if "error" in b:
                res.append({"page": i + 1, "half": b["half"], "error": b["error"]}); continue
            r = check_block(b); r.update({"page": i + 1, "half": "LR"[b["half"]], "months_found": len(b["months"])})
            r["cells"] = [c for row in [b["rows"]["open"]] + b["months"] + [b["rows"].get("total", {}), b["rows"].get("b31", {})] for c in row.values()]
            res.append(r)
        print("page", i + 1, round(time.time() - t0, 1), "s", flush=True)
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    for r in res:
        if "error" in r: print(r["page"], r["half"], r["error"]); continue
        rr = r["rules"]
        print("p%d %s months=%d  raw %d/%d  after %d/%d  corrections %d  review %d  notes %d" % (
            r["page"], r["half"], r["months_found"], sum(v["raw"] for v in rr.values()), len(rr),
            sum(v["after"] for v in rr.values()), len(rr), len(r["corrections"]), len(r["review"]), r["notes"]))


if __name__ == "__main__":
    main()
