/* plan-core.js — renderer-agnostic greenfield land-use plan for Lucknow AI City.
   Pure logic: real building footprints -> per-cell vacancy -> largest contiguous
   developable cluster -> land-use zoning -> planned massing + trees.
   Consumed by BOTH the deck.gl City-Scan view and the Cesium app.
   Requires global `h3` (h3-js v4). No external I/O. */
(function (root) {
  const AOI = [[80.9424,26.7434],[80.9755,26.7434],[80.9755,26.7725],[80.9424,26.7725],[80.9424,26.7434]];
  const ZONES = {
    ai:    { name:"AI district",       col:[57,198,214],  h:[30,55] },
    innov: { name:"Innovation & labs", col:[226,106,156], h:[26,42] },
    comm:  { name:"Commercial",        col:[232,184,75],  h:[15,26] },
    resi:  { name:"Residential",       col:[214,203,180], h:[9,16]  },
    civic: { name:"Civic & plaza",     col:[240,196,90],  h:[6,10]  },
    green: { name:"Parks & green",     col:[70,192,122],  h:[0,0]   },
  };
  // deterministic PRNG (no Math.random — keeps the plan stable across reloads/renderers)
  function mulberry(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296;};}
  function ringOf(f){let c=f.geometry.coordinates;while(Array.isArray(c)&&c.length&&Array.isArray(c[0])&&Array.isArray(c[0][0]))c=c[0];return c;}
  function centroid(r){let x=0,y=0;for(const p of r){x+=p[0];y+=p[1];}return[x/r.length,y/r.length];}

  // Largest connected component among `cells` using H3 gridDisk adjacency.
  function largestCluster(cells){
    const S=new Set(cells), seen=new Set(); let best=[];
    for(const c of cells){
      if(seen.has(c)) continue;
      const comp=[], q=[c]; seen.add(c);
      while(q.length){ const x=q.pop(); comp.push(x);
        for(const nb of h3.gridDisk(x,1)){ if(S.has(nb)&&!seen.has(nb)){ seen.add(nb); q.push(nb); } } }
      if(comp.length>best.length) best=comp;
    }
    return best;
  }

  // buildings: GeoJSON FeatureCollection with properties.area (m²). opts.aoi = a [[lng,lat],…] ring
  // (defaults to the built-in demo AOI). Returns the full plan. Renderer-agnostic + city-agnostic.
  function computePlan(buildings, opts){
    opts = opts || {};
    const RES = 9, VAC = opts.vacancyMax != null ? opts.vacancyMax : 0.02;
    const aoiRing = (opts.aoi && opts.aoi.length >= 4) ? opts.aoi : AOI;
    const cells = h3.polygonToCells([aoiRing], RES, true);
    const area = {}, built = {};
    for(const c of cells){ area[c]=h3.cellArea(c,'m2'); built[c]=0; }
    for(const f of (buildings.features||[])){
      const ctr = centroid(ringOf(f));
      const c = h3.latLngToCell(ctr[1], ctr[0], RES);
      if(built[c]!=null) built[c] += (f.properties && f.properties.area) || 30;
    }
    const cov = {}; for(const c of cells) cov[c] = built[c]/area[c];
    const vacant = cells.filter(c => cov[c] <= VAC);
    // greenfield = the largest CONTIGUOUS block of vacant cells (the parcel we plan)
    const cluster = largestCluster(vacant);
    const others  = vacant.filter(c => cluster.indexOf(c) < 0);

    // zone the cluster by distance from its centroid (+ a central civic/road spine)
    const ll = cluster.map(c=>h3.cellToLatLng(c));
    const mlat = ll.reduce((s,p)=>s+p[0],0)/ll.length, mlng = ll.reduce((s,p)=>s+p[1],0)/ll.length;
    const withD = cluster.map(c=>{const[la,ln]=h3.cellToLatLng(c);return {c,la,ln,d:Math.hypot(la-mlat,ln-mlng)};})
                         .sort((a,b)=>a.d-b.d);
    const n = withD.length;
    // ring-based bands (core AI → innovation → commercial → residential → green edge),
    // robust for a small parcel so every land use is represented; one civic cell just off core.
    withD.forEach((o,i)=>{ const f=i/n;
      o.zone = f<0.28?"ai" : f<0.46?"innov" : f<0.64?"comm" : f<0.85?"resi" : "green";
    });
    if(n>=6) withD[Math.min(1,n-1)].zone="civic";

    // planned massing + trees inside each zoned cell (deterministic)
    const rng = mulberry(1337), mass = [], trees = [];
    for(const o of withD){ const Z=ZONES[o.zone], cx=o.ln, cy=o.la;
      if(o.zone==="green"){ for(let k=0;k<8;k++) trees.push({p:[cx+(rng()-0.5)*0.0017, cy+(rng()-0.5)*0.0017]}); continue; }
      const nb = o.zone==="ai"?3 : o.zone==="innov"?1 : o.zone==="civic"?1 : 4;
      for(let k=0;k<nb;k++){
        const bx=cx+(rng()-0.5)*0.0016, by=cy+(rng()-0.5)*0.0016,
              s=(o.zone==="ai"?0.00034:0.00024)*(0.7+rng()*0.6);
        mass.push({ poly:[[bx-s,by-s],[bx+s,by-s],[bx+s,by+s],[bx-s,by+s],[bx-s,by-s]],
                    h:Z.h[0]+(Z.h[1]-Z.h[0])*rng(), zone:o.zone, col:Z.col });
      }
    }
    const counts={}; withD.forEach(o=>counts[o.zone]=(counts[o.zone]||0)+1);
    return {
      zones: ZONES,
      dev:    withD.map(o=>({ poly:h3.cellToBoundary(o.c,true), zone:o.zone, c:o.c, center:[o.ln,o.la] })),
      reserve: others.map(c=>({ poly:h3.cellToBoundary(c,true), c })), // vacant but outside the plan
      mass, trees, counts,
      stats: { aoiCells:cells.length, vacant:vacant.length, developable:cluster.length }
    };
  }

  const api = { computePlan, ZONES, AOI };
  root.LandUsePlan = api;      // generic name (City Scan, any city)
  root.LucknowPlan = api;      // back-compat alias for the standalone prototypes
})(typeof window!=="undefined" ? window : this);
