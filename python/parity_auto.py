import os, sys, json
sys.path.insert(0, r"C:\Users\Timo\Downloads\railway-deploy")
import networkx as nx
from shapely.geometry import Point
from shapely import STRtree, make_valid
from shapely.ops import unary_union, transform as shapely_transform
from osmnx.distance import nearest_nodes
import isochrone_areas as areas
from build_focus_isochrones import (load_networks, add_drive_time, _set_time_minutes,
    _buffer_union, _fill_holes, simplify, EDGE_BUFF, WALK_SPEED_KMH, _TO_RD, _TO_WGS84)
POP_FLOOR=50; VIEWER_DIR=areas.OUT_DIR
def _sm():
    seen,out=set(),[]
    for a in areas.AREAS:
        for s in json.load(open(os.path.join(VIEWER_DIR,f"{a['slug']}_supermarkten.json"),encoding="utf-8")):
            k=f"{s['naam']}|{s['adres']}"
            if k in seen: continue
            seen.add(k); x,y=_TO_RD.transform(s["lon"],s["lat"]); out.append({"wvo":int(s["wvo"] or 0),"x":x,"y":y})
    return out
def _bag():
    seen,xs,ys,ps=set(),[],[],[]
    for a in areas.AREAS:
        for b in json.load(open(os.path.join(VIEWER_DIR,f"{a['slug']}_bag_adressen.json"),encoding="utf-8")):
            k=(b["x"],b["y"])
            if k in seen: continue
            seen.add(k); xs.append(b["x"]); ys.append(b["y"]); ps.append(float(b["pop"]))
    return ps, STRtree([Point(x,y) for x,y in zip(xs,ys)])
def _bt():
    bt,gs=[],[]
    for a in areas.AREAS:
        for _,r in areas.area_buurten(a).iterrows():
            bt.append({"c":r["buurtcode"],"i":max(0,int(r["aantal_inwoners"]))}); gs.append(make_valid(r.geometry))
    return bt,gs,STRtree(gs)
def kpi(S,poly):
    wvo=sum(S["sm"][i]["wvo"] for i in S["smt"].query(poly,predicate="contains"))
    pb=sum(S["bp"][i] for i in S["bgt"].query(poly,predicate="contains"))
    pa=0.0
    for i in S["btt"].query(poly,predicate="intersects"):
        inter=poly.intersection(S["bg"][i]).area
        if inter>0: pa+=S["bi"][i]*(inter/S["ba"][i])
    return round(wvo,1),round(pb,1),round(pa,1)
import time; t0=time.time()
region=unary_union([areas.area_boundary(a) for a in areas.AREAS])
dG,wG=load_networks(region,areas.DRIVE_MARGIN,areas.WALK_MARGIN); dG=add_drive_time(dG); wG=_set_time_minutes(wG,WALK_SPEED_KMH)
S={}; S["sm"]=_sm(); S["smt"]=STRtree([Point(s["x"],s["y"]) for s in S["sm"]])
S["bp"],S["bgt"]=_bag(); bt,gs,bt_tree=_bt(); S["bg"],S["btt"]=gs,bt_tree; S["bi"]=[b["i"] for b in bt]; S["ba"]=[g.area for g in gs]
print(f"loaded {time.time()-t0:.0f}s",flush=True)
for (lon,lat) in [(5.198,52.066),(5.121,52.09),(5.164,52.088),(5.292,52.132),(5.42,51.975),(5.343,51.97)]:
    x,y=_TO_RD.transform(lon,lat); dn=nearest_nodes(dG,[x],[y])[0]
    for mt in (10,20):
        dmap=nx.single_source_dijkstra_path_length(dG,dn,weight="time")
        reach=set(n for n,d in dmap.items() if d<=mt)
        poly=_buffer_union(dG,EDGE_BUFF,node_filter=reach); poly=_fill_holes(simplify(poly)) if (poly is not None and not poly.is_empty) else None
        w,pb,pa=kpi(S,poly) if poly is not None else (0.0,0.0,0.0)
        print(f"AUTO {lon},{lat} {mt}min wvo={w} pop_bag={pb} pop_area={pa}",flush=True)
