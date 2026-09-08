/* interiors.js — click a building → the camera glides inside for a scripted walk-through, interior + path
   varying BY BUILDING TYPE. No external models (glTF needs workers/egress the sandbox blocks): the interior is
   generated procedurally from SYNCHRONOUS Cesium primitives (curtain-wall shell + facade fins, podium, floor
   slabs with edge beams + atrium void, a service core, type-specific props). Illustrative, not a real BIM model.
   Global `Interiors`. Configure once: Interiors.config({viewer, scene, cityPrims, setOrbit, hud, exitBtn}). */
(function (root) {
  let V, SCENE, CITY, setOrbit, HUD, EXITBTN;
  let PRIM = [], active = null, exiting = false, tickRemover = null, saved = null;
  let GZ = 0;   // ground elevation at the building — interior + camera are lifted onto the terrain (0 tokenless)

  const rgba = (a, al) => Cesium.Color.fromBytes(a[0], a[1], a[2], Math.round((al == null ? 1 : al) * 255));
  const rad = Cesium.Math.toRadians;
  const VF = () => Cesium.PerInstanceColorAppearance.VERTEX_FORMAT;
  const ring = (cx, cy, hw, hh) => [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh], [cx - hw, cy + hh], [cx - hw, cy - hh]];
  const degArr = r => { const f = []; for (const p of r) f.push(p[0], p[1]); return Cesium.Cartesian3.fromDegreesArray(f); };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const mPerDegLat = 111320;
  const mPerDegLon = lat => 111320 * Math.cos(rad(lat));

  function slab(r, z, col, al, hole) {   // flat polygon at altitude z, optional central hole (atrium)
    const h = new Cesium.PolygonHierarchy(degArr(r), hole ? [new Cesium.PolygonHierarchy(degArr(hole))] : []);
    return new Cesium.GeometryInstance({ geometry: new Cesium.PolygonGeometry({ polygonHierarchy: h, height: z + GZ, vertexFormat: VF() }),
      attributes: { color: Cesium.ColorGeometryInstanceAttribute.fromColor(rgba(col, al)) } });
  }
  function boxRing(r, z0, z1, col, al) {  // extruded box over a footprint ring
    return new Cesium.GeometryInstance({ geometry: new Cesium.PolygonGeometry({ polygonHierarchy: new Cesium.PolygonHierarchy(degArr(r)), height: z0 + GZ, extrudedHeight: z1 + GZ, vertexFormat: VF() }),
      attributes: { color: Cesium.ColorGeometryInstanceAttribute.fromColor(rgba(col, al)) } });
  }
  const box = (cx, cy, hw, hh, z0, z1, col, al) => boxRing(ring(cx, cy, hw, hh), z0, z1, col, al);
  function paint(insts, translucent, shadows) {
    if (!insts.length) return;
    const pr = SCENE.primitives.add(new Cesium.Primitive({ asynchronous: false, geometryInstances: insts,
      appearance: new Cesium.PerInstanceColorAppearance({ flat: false, closed: !translucent, translucent: !!translucent }),
      shadows: shadows == null ? Cesium.ShadowMode.DISABLED : shadows }));
    PRIM.push(pr); return pr;
  }

  // type → recipe: floor height, atrium?, prop style, accent + glass + structure colours, label
  const RECIPES = {
    tower:    { fh: 4.0, atrium: true,  props: "open",  accent: [96, 206, 230],  glass: [104, 196, 226], struct: [58, 70, 100], label: "AI / office tower" },
    office:   { fh: 3.6, atrium: true,  props: "open",  accent: [232, 190, 96],  glass: [214, 188, 128], struct: [70, 66, 60],  label: "Commercial office" },
    lab:      { fh: 4.4, atrium: false, props: "lab",   accent: [228, 124, 168], glass: [206, 158, 184], struct: [66, 58, 74],  label: "Innovation lab" },
    resi:     { fh: 3.2, atrium: false, props: "resi",  accent: [224, 176, 120], glass: [206, 196, 176], struct: [78, 72, 64],  label: "Residences" },
    civic:    { fh: 6.0, atrium: true,  props: "hall",  accent: [242, 208, 124], glass: [226, 208, 156], struct: [86, 78, 60],  label: "Civic hall" },
    landmark: { fh: 6.0, atrium: true,  props: "obs",   accent: [150, 232, 250], glass: [150, 216, 236], struct: [70, 90, 110], label: "Signature landmark" },
    utility:  { fh: 4.5, atrium: false, props: "plant", accent: [150, 160, 175], glass: [150, 160, 175], struct: [80, 84, 92],  label: "Utility / plant" },
    datacentre:{ fh: 5.0, atrium: false, props: "lab",  accent: [92, 176, 206],  glass: [120, 182, 206], struct: [58, 72, 92],  label: "Data centre" },
  };

  // build a richer procedural interior; returns meta for the walk-through
  function buildInterior(b, R) {
    const { cx, cy, hw, hh, h } = b;
    const N = clamp(Math.round(h / R.fh), 2, 26);
    const iw = hw * 0.88, ih = hh * 0.88;                      // interior footprint (inside the shell)
    const holeR = R.atrium ? ring(cx, cy, hw * 0.28, hh * 0.28) : null;
    const dark = [R.struct[0] * 0.5 | 0, R.struct[1] * 0.5 | 0, R.struct[2] * 0.5 | 0];

    // 1) PODIUM / entrance base (wider, ~1.5 floors) with a canopy lip — grounds the tower
    paint([box(cx, cy, hw * 1.06, hh * 1.06, 0, R.fh * 1.4, R.struct, 0.96)], false, Cesium.ShadowMode.RECEIVE_ONLY);
    paint([slab(ring(cx, cy, hw * 1.14, hh * 1.14), R.fh * 1.42, dark, 0.95)], false);   // canopy lip
    // an entrance notch (brighter) on the south face
    paint([box(cx, cy - hh * 1.02, hw * 0.34, hh * 0.10, 0, R.fh * 1.1, R.accent, 0.9)], false);

    // 2) CURTAIN-WALL glass shell (translucent so we can see in)
    paint([boxRing(ring(cx, cy, hw, hh), R.fh * 1.4, h, R.glass, 0.12)], true);
    // 3) VERTICAL FACADE FINS on each face — reads as a real facade, not a bare box
    const fins = [], nf = 6;
    for (let i = 0; i <= nf; i++) {
      const fx = cx - hw + (2 * hw) * (i / nf), fy = cy - hh + (2 * hh) * (i / nf);
      fins.push(box(fx, cy - hh, hw * 0.015, hh * 0.02, R.fh * 1.4, h, R.struct, 0.85)); // south
      fins.push(box(fx, cy + hh, hw * 0.015, hh * 0.02, R.fh * 1.4, h, R.struct, 0.85)); // north
      fins.push(box(cx - hw, fy, hw * 0.02, hh * 0.015, R.fh * 1.4, h, R.struct, 0.85)); // west
      fins.push(box(cx + hw, fy, hw * 0.02, hh * 0.015, R.fh * 1.4, h, R.struct, 0.85)); // east
    }
    paint(fins, false);

    // 4) FLOOR SLABS with a darker edge-beam rim; atrium void punched above the lobby
    const slabs = [], rims = [];
    for (let i = 1; i < N; i++) {
      const z = R.fh * 1.4 + (i - 1) * R.fh;
      if (z > h - 0.5) break;
      rims.push(slab(ring(cx, cy, iw * 1.02, ih * 1.02), z - 0.25, dark, 0.95, holeR));
      slabs.push(slab(ring(cx, cy, iw, ih), z, [58, 66, 92], 0.96, holeR));
    }
    paint(rims, false); paint(slabs, false);
    // lobby floor (polished)
    paint([slab(ring(cx, cy, iw, ih), R.fh * 1.4, [40, 46, 66], 0.98)], false);

    // 5) SERVICE CORE (lifts/stairs) — solid full-height block, offset so it doesn't fill the atrium
    const coff = R.atrium ? 0.55 : 0;
    paint([box(cx + iw * coff, cy + ih * coff, hw * 0.14, hh * 0.14, R.fh * 1.4, h, R.struct, 0.98)], false, Cesium.ShadowMode.ENABLED);

    // 6) TYPE-SPECIFIC props on two representative floors
    const f1 = clamp(Math.round(N * 0.4), 2, N - 1), f2 = clamp(Math.round(N * 0.7), 2, N - 1);
    const props = [];
    const Z = i => R.fh * 1.4 + (i - 1) * R.fh;
    const grid = (z, nx, ny, sw, sh, ph, col) => { for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) {
      const px = cx + (nx === 1 ? 0 : (i / (nx - 1) - 0.5)) * iw * 1.3, py = cy + (ny === 1 ? 0 : (j / (ny - 1) - 0.5)) * ih * 1.3;
      if (R.atrium && Math.abs(px - cx) < hw * 0.3 && Math.abs(py - cy) < hh * 0.3) continue;   // keep the atrium clear
      props.push(box(px, py, sw, sh, z + 0.1, z + ph, col, 1)); } };
    for (const fl of [f1, f2]) { const z = Z(fl);
      if (R.props === "open") grid(z, 4, 3, hw * 0.08, hh * 0.05, 1.1, R.accent);            // desk clusters
      else if (R.props === "lab") grid(z, 2, 4, hw * 0.13, hh * 0.05, 1.5, R.accent);         // equipment benches
      else if (R.props === "resi") { grid(z, 2, 3, hw * 0.20, hh * 0.16, 0.6, R.accent);       // apartment pods
        props.push(box(cx, cy, hw * 0.025, hh * 0.9, z + 0.1, z + 2.4, R.struct, 0.9)); }      // corridor partition
      else if (R.props === "hall") props.push(box(cx, cy, iw * 0.66, ih * 0.42, z + 0.1, z + 0.9, R.accent, 0.95)); // seating tier
      else if (R.props === "plant") grid(z, 2, 2, hw * 0.16, hh * 0.16, 2.0, R.accent);         // machinery
    }
    // observation deck ring near the top for landmark/tower
    if (R.props === "obs" || (R.atrium && b.type === "tower")) {
      const zt = h - R.fh * 0.8;
      props.push(slab(ring(cx, cy, iw, ih), zt, R.accent, 0.45));
      const rail = [];
      for (const sx of [-1, 1]) rail.push(box(cx + sx * iw, cy, hw * 0.02, ih, zt, zt + 1.1, [220, 235, 250], 0.85));
      for (const sy of [-1, 1]) rail.push(box(cx, cy + sy * ih, hw, hh * 0.02, zt, zt + 1.1, [220, 235, 250], 0.85));
      props.push(...rail);
    }
    // rooftop cap / crown
    paint([box(cx, cy, hw * 0.5, hh * 0.5, h, h + R.fh * 0.6, R.accent, 0.9)], false);
    paint(props, false);
    return { N, fh: R.fh, base: R.fh * 1.4, label: R.label, atrium: R.atrium, props: R.props };
  }

  // absolute compass bearing (deg) from (lon,lat) → (tlon,tlat)
  function bearing(lon, lat, tlon, tlat) {
    const y = Math.sin(rad(tlon - lon)) * Math.cos(rad(tlat));
    const x = Math.cos(rad(lat)) * Math.sin(rad(tlat)) - Math.sin(rad(lat)) * Math.cos(rad(tlat)) * Math.cos(rad(tlon - lon));
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }
  // move (lon,lat) by (east,north) metres
  const move = (lon, lat, e, n) => [lon + e / mPerDegLon(lat), lat + n / mPerDegLat];

  // scripted interior fly-through — smooth, well-framed, type-specific
  function path(b, m) {
    const { cx, cy, h } = b;
    const wm = b.hw * mPerDegLon(cy), hm = b.hh * mPerDegLat;   // half-extents in metres
    const P = [];
    const look = (lon, lat, z, pt, d) => P.push({ lon, lat, z, hd: bearing(lon, lat, cx, cy), pt, d });
    // 2) lobby — enter at the podium, look in and (if present) up the atrium
    const [dl, dt] = move(cx, cy, 0, -hm * 0.55);
    look(dl, dt, m.base + 2.2, m.atrium ? 24 : 6, 3.2);
    if (m.props === "lab" || m.props === "resi" || m.props === "plant") {
      // corridor dolly across a working floor — steady, level
      const z = m.base + (Math.max(1, Math.round(m.N * 0.4)) - 1) * m.fh + 1.6;
      const [al, at] = move(cx, cy, -wm * 0.75, 0), [bl, bt] = move(cx, cy, wm * 0.75, 0);
      P.push({ lon: al, lat: at, z, hd: 90, pt: 0, d: 2.8 });
      P.push({ lon: bl, lat: bt, z, hd: 90, pt: 0, d: 3.6 });
    } else {
      // rise through the atrium / open floors
      const z1 = m.base + (Math.max(1, Math.round(m.N * 0.45)) - 1) * m.fh + 1.6;
      const [rl, rt] = move(cx, cy, -wm * 0.15, -hm * 0.15);
      look(rl, rt, z1, m.atrium ? 10 : 2, 3.6);
      const z2 = m.base + (Math.max(1, Math.round(m.N * 0.65)) - 1) * m.fh + 1.6;
      const [pl, pt] = move(cx, cy, wm * 0.45, hm * 0.2);
      P.push({ lon: pl, lat: pt, z: z2, hd: (bearing(pl, pt, cx, cy) + 55) % 360, pt: 0, d: 3.4 });
    }
    // top / observation — rise, gentle downward look over the district
    look(cx, cy, Math.max(m.base, h - m.fh * 0.6), (m.props === "obs" ? -4 : -10), 3.8);
    return P;
  }

  function flyStep(s, next) {
    V.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(s.lon, s.lat, s.z + GZ),
      orientation: { heading: rad(s.hd), pitch: rad(s.pt), roll: 0 },
      duration: s.d, easingFunction: Cesium.EasingFunction.QUADRATIC_IN_OUT, complete: next });
  }
  function run(b, m) {
    // 1) clean establishing shot — frame the whole building from outside (no bounce: pure flyToBoundingSphere)
    const radM = Math.max(b.hw * mPerDegLon(b.cy), b.hh * mPerDegLat) * 2.6 + b.h;
    const bs = new Cesium.BoundingSphere(Cesium.Cartesian3.fromDegrees(b.cx, b.cy, b.h * 0.55 + GZ), radM);
    const P = path(b, m); let i = 0;
    const step = () => { if (exiting || i >= P.length) return interiorOrbit(b, m); flyStep(P[i++], step); };
    V.camera.flyToBoundingSphere(bs, { offset: new Cesium.HeadingPitchRange(rad(28), rad(-24), radM * 1.7),
      duration: 2.4, easingFunction: Cesium.EasingFunction.QUADRATIC_IN_OUT, complete: step });
  }

  // gentle interior orbit after the tour (so it doesn't freeze); released on exit
  function interiorOrbit(b, m) {
    if (exiting) return;
    const centre = Cesium.Cartesian3.fromDegrees(b.cx, b.cy, m.base + (m.N * m.fh) * 0.45 + GZ);
    const rangeM = Math.max(45, Math.max(b.hw * mPerDegLon(b.cy), b.hh * mPerDegLat) * 3.2);
    let hd = 20;
    const tick = () => { hd = (hd + 0.05) % 360; V.camera.lookAt(centre, new Cesium.HeadingPitchRange(rad(hd), rad(-12), rangeM)); };
    V.clock.onTick.addEventListener(tick);
    tickRemover = () => { V.clock.onTick.removeEventListener(tick); V.camera.lookAtTransform(Cesium.Matrix4.IDENTITY); };
  }

  // terrain elevation (m above ellipsoid) at the building — 0 when there's no terrain (tokenless / ellipsoid).
  // With World Terrain the ground sits ~120 m up at Lucknow; geometry built at 0 would be underground.
  function groundZ(b) {
    try {
      const tp = V.terrainProvider;
      if (!tp || tp instanceof Cesium.EllipsoidTerrainProvider) return 0;
      const h = SCENE.globe.getHeight(Cesium.Cartographic.fromDegrees(b.cx, b.cy));
      return (typeof h === "number" && isFinite(h)) ? h : 0;
    } catch (e) { return 0; }
  }

  function enter(b) {
    if (active || !b || b.cx == null) return;
    active = b; exiting = false;
    const R = RECIPES[b.type] || RECIPES.office;
    GZ = groundZ(b);   // lift the whole interior + camera onto the terrain (photoreal); 0 when tokenless
    setOrbit && setOrbit(false);
    // CRITICAL for a clean zoom (no bounce): release the city orbit's look-at transform, and relax the camera
    // controller — minimumZoomDistance/collision detection were shoving the camera back up at interior heights.
    V.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    const c = V.scene.screenSpaceCameraController;
    saved = { min: c.minimumZoomDistance, coll: c.enableCollisionDetection };
    c.minimumZoomDistance = 1; c.enableCollisionDetection = false;
    CITY && CITY.forEach(p => { try { p.show = false; } catch (e) {} });
    const m = buildInterior(b, R);
    if (HUD) { HUD.hidden = false; HUD.innerHTML =
      `<div class="k">Inside · ${R.label}</div><div class="n">${m.N} floors · illustrative interior</div>`
      + `<div class="h">Walk-through in progress — drag to look around · Esc / Exit to leave</div>`; }
    if (EXITBTN) EXITBTN.hidden = false;
    run(b, m);
  }

  function exit() {
    if (!active) return;
    exiting = true;
    if (tickRemover) { tickRemover(); tickRemover = null; }
    try { V.camera.cancelFlight(); } catch (e) {}
    try { V.camera.lookAtTransform(Cesium.Matrix4.IDENTITY); } catch (e) {}
    PRIM.forEach(p => { try { SCENE.primitives.remove(p); } catch (e) {} });
    PRIM = [];
    if (saved) { const c = V.scene.screenSpaceCameraController; c.minimumZoomDistance = saved.min; c.enableCollisionDetection = saved.coll; saved = null; }
    CITY && CITY.forEach(p => { try { p.show = true; } catch (e) {} });
    if (HUD) HUD.hidden = true;
    if (EXITBTN) EXITBTN.hidden = true;
    active = null;
    setOrbit && setOrbit(true);        // resume city orbit (re-frames the district)
    SCENE.requestRender();
  }

  root.Interiors = {
    config({ viewer, scene, cityPrims, setOrbit: so, hud, exitBtn }) {
      V = viewer; SCENE = scene; CITY = cityPrims || []; setOrbit = so; HUD = hud; EXITBTN = exitBtn;
      if (EXITBTN) EXITBTN.onclick = exit;
      document.addEventListener("keydown", e => { if (e.key === "Escape" && active) exit(); });
    },
    enter, exit, isActive: () => !!active,
  };
})(typeof window !== "undefined" ? window : this);
