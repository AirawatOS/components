"""Local grid finding for ruled register pages whose lines bend a little (bound books): vertical lines are found in
horizontal tiles and followed down the page; horizontal lines are found inside each column strip and linked across
neighbouring columns. Returns, per half page, a list of columns, each a list of (y_top, y_bottom) row intervals with
the column's x range at that height."""
import cv2, numpy as np

def masks(gray):
    bw = cv2.adaptiveThreshold(255 - gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 21, -4)
    H, W = bw.shape
    hor = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1)))
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 30)))
    ink = cv2.subtract(bw, cv2.dilate(cv2.bitwise_or(hor, ver), np.ones((5, 5), np.uint8)))
    return hor, ver, ink

def peaks(s, thr, merge=6):
    idx = np.where(s > thr)[0]; out = []
    for i in idx:
        if out and i - out[-1][-1] <= merge: out[-1].append(i)
        else: out.append([i])
    return [float(np.mean(g)) for g in out]

def vlines(ver, x0, x1, y0, y1):
    s = ver[y0:y1, x0:x1].sum(0) / 255.0 / (y1 - y0)
    return [x + x0 for x in peaks(s, 0.55)]

def columns(ver, x0, x1, H, tile=160):
    """Track vertical lines down the half page: {x at tile centre} per tile, linked to the line found above."""
    tracks = []                                   # each: list of (y_centre, x)
    for y0 in range(0, H - tile, tile // 2):
        xs = vlines(ver, x0, x1, y0, y0 + tile); yc = y0 + tile / 2
        for x in xs:
            best = min(tracks, key=lambda t: abs(t[-1][1] - x), default=None)
            if best is not None and abs(best[-1][1] - x) < 12 and yc - best[-1][0] <= 3 * tile: best.append((yc, x))
            else: tracks.append([(yc, x)])
    tracks = [t for t in tracks if t[-1][0] - t[0][0] > H * 0.35]          # long ruled lines only
    tracks.sort(key=lambda t: np.mean([x for _, x in t]))
    return tracks

def x_at(track, y):
    ys = [a for a, _ in track]; xs = [b for _, b in track]
    return float(np.interp(y, ys, xs))

def hlines_in_strip(hor, xa, xb, y0, y1):
    s = hor[int(y0):int(y1), int(xa):int(xb)].sum(1) / 255.0 / max(1, (xb - xa))
    return [y + y0 for y in peaks(s, 0.6, merge=4)]

def grid_half(hor, ver, x0, x1, H):
    tr = columns(ver, x0, x1, H)
    cols = []
    for a, b in zip(tr, tr[1:]):
        ytop = max(a[0][0], b[0][0]) - 80; ybot = min(a[-1][0], b[-1][0]) + 80
        ys = []
        # horizontal lines inside the strip, measured in short height windows so the strip edges follow the bend
        for wy in range(int(max(0, ytop)), int(min(H, ybot)), 120):
            xa = x_at(a, wy + 60) + 4; xb = x_at(b, wy + 60) - 4
            if xb - xa < 25: continue
            for y in hlines_in_strip(hor, xa, xb, wy, min(H, wy + 120)):
                if not ys or y - ys[-1] > 6: ys.append(y)
        cols.append({"left": a, "right": b, "lines": ys})
    return cols

def anchor_rows(cols, min_h=14):
    k = max(range(len(cols)), key=lambda i: len(cols[i]["lines"]))
    L = cols[k]["lines"]
    return L, [(a, b) for a, b in zip(L, L[1:]) if b - a >= min_h]

def const_track(x, H): return [(0.0, float(x)), (float(H), float(x))]

def cells_from(strips, cols_lines, L, rows):
    """strips: [(name, left_track, right_track, own ruled lines)] → cells for every row; rows are the anchor strip's
    intervals carried across with each strip's own offset (the page bends)."""
    out = []
    def offset_fn(lines):
        pairs = [(y, min(lines, key=lambda z: abs(z - y))) for y in L] if lines else []
        pairs = [(y, z - y) for y, z in pairs if abs(z - y) <= 10]
        if not pairs: return lambda y: 0.0
        ys, ds = zip(*pairs); return lambda y: float(np.interp(y, ys, ds))
    for name, lt, rt, lines in strips:
        off = offset_fn(lines)
        for r, (a, b) in enumerate(rows):
            ya, yb = a + off(a), b + off(b); ym = (ya + yb) / 2
            out.append({"row": r, "col": name, "box": [int(x_at(lt, ym)), int(ya), int(x_at(rt, ym)), int(yb)]})
    return out
