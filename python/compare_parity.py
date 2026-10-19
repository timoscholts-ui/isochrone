# -*- coding: utf-8 -*-
"""Diff parity_python_fast.json (Python reference) vs parity_rust.json (Rust server)."""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
py = json.load(open(os.path.join(HERE, "parity_python_fast.json")))
rs = json.load(open(os.path.join(HERE, "parity_rust.json")))
rs_by_q = {tuple(r["query"].items()): r for r in rs}


def pct(a, b):
    if a == b == 0:
        return 0.0
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b) * 100.0


print(f"{'query':44s} {'wvo_py':>9s} {'wvo_rs':>9s} {'popbag%':>8s} {'poparea%':>9s} {'bbox_m':>7s}")
rows = []
for r in py:
    q = r["query"]
    key = tuple(q.items())
    rr = rs_by_q.get(key)
    if rr is None:
        print(f"{q}  MISSING in rust")
        continue
    kp, kr = r["kpi"], rr["kpi"]
    # bbox diff in metres (WGS84 lon/lat)
    bp, br = r["geometry"], rr["geometry"]
    bbox_m = None
    if bp and br:
        dx = max(abs(bp["bbox"][i] - br["bbox"][i]) for i in (0, 2)) * 111_320 * math.cos(math.radians(q["lat"]))
        dy = max(abs(bp["bbox"][i] - br["bbox"][i]) for i in (1, 3)) * 110_574
        bbox_m = math.hypot(dx, dy)
    popbag = pct(kr["pop_bag"], kp["pop_bag"])
    poparea = pct(kr["pop_area"], kp["pop_area"])
    rows.append((q, kp, kr, popbag, poparea, bbox_m))
    print(f"{str(q):44s} {kp['wvo']:9.1f} {kr['wvo']:9.1f} {popbag:7.2f}% {poparea:8.2f}% {bbox_m if bbox_m is None else round(bbox_m,1):>7s}")

# summary
print("\n=== summary ===")
pb = [r[3] for r in rows if not math.isinf(r[3])]
pa = [r[4] for r in rows if not math.isinf(r[4])]
bb = [r[5] for r in rows if r[5] is not None]
print(f"pop_bag %diff:  median={sorted(pb)[len(pb)//2]:.2f}%  max={max(pb):.2f}%  (n={len(pb)})")
print(f"pop_area %diff: median={sorted(pa)[len(pa)//2]:.2f}%  max={max(pa):.2f}%  (n={len(pa)})")
print(f"bbox diff (m):  median={sorted(bb)[len(bb)//2]:.1f}  max={max(bb):.1f}  (n={len(bb)})")
