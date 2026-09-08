// layers/network.js — ONE generic layer for every infrastructure network (power/water/fibre/…). Edges = polylines,
// nodes = pickable points (id {kind:"netnode",id} → the twin selects them). Built hidden; setActive(list) toggles
// which kinds are visible. Node positions are fractional [fx,fy] within the district bbox, scaled here.
export function createNetworkLayer({ scene, bbox, networks, hideable }) {
  const [W, S, , N] = bbox, dx = bbox[2] - W, dy = N - S;
  const abs = f => [W + f[0] * dx, S + f[1] * dy];
  const col = (c, a) => Cesium.Color.fromBytes(c[0], c[1], c[2], Math.round((a == null ? 1 : a) * 255));
  const groups = {};   // kind → { pl, pts }
  const nodeXY = {};   // nodeId → [lon,lat]  (shared, for lookups)

  for (const net of networks) {
    net.nodes.forEach(n => { nodeXY[n.id] = abs(n.f); });
    const pl = new Cesium.PolylineCollection();
    for (const [a, b] of net.edges) {
      const pa = nodeXY[a], pb = nodeXY[b]; if (!pa || !pb) continue;
      pl.add({ width: 2.6, positions: Cesium.Cartesian3.fromDegreesArrayHeights([pa[0], pa[1], 6, pb[0], pb[1], 6]),
        material: Cesium.Material.fromType("Color", { color: col(net.color, 0.9) }) });
    }
    pl.show = false; scene.primitives.add(pl); hideable && hideable(pl);
    const pts = new Cesium.PointPrimitiveCollection(); pts.show = false; scene.primitives.add(pts); hideable && hideable(pts);
    for (const n of net.nodes) {
      const p = nodeXY[n.id], hub = /substation|reservoir|core/.test(n.type || "");
      pts.add({ position: Cesium.Cartesian3.fromDegrees(p[0], p[1], 8), color: col(net.color, 1),
        pixelSize: hub ? 14 : 9, outlineColor: Cesium.Color.fromBytes(10, 16, 30, 235), outlineWidth: 2,
        id: { kind: "netnode", id: n.id } });
    }
    groups[net.kind] = { pl, pts };
  }

  return {
    nodeXY,
    setActive(list) {
      for (const k in groups) { const on = list.indexOf(k) >= 0; groups[k].pl.show = on; groups[k].pts.show = on; }
      scene.requestRender();
    },
  };
}
