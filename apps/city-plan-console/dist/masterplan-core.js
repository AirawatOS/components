/* masterplan-core.js — the LINKED masterplan model for an AI-City greenfield.
   Pure, deterministic. Takes the developable land-use (area by zone, from plan-core) + a scenario of
   assumptions, and computes the whole interdependent chain:
     land-use -> capacity -> {water, power, sewage, mobility} demand -> infrastructure sizing
              -> capex -> revenue -> financing (DSCR/IRR/payback) -> net-zero energy+carbon balance
   Every default assumption carries a SOURCE (norm/reference). Change one input -> the chain recomputes.
   Consumed by the City Plan Console (and, later, by certified engines that persist each stage as governed facts).
   All money in INR crore (1 cr = 10 million). Illustrative planning model — not a sanctioned DPR. */
(function (root) {
  // ---- ASSUMPTIONS (defaults; each a {v:value, u:unit, src:source}). Scenario overrides by key. ----
  const A = {
    // massing
    far_ai:      {v:3.5,  u:"FAR",         src:"UPAVP IT-park norms (indicative)"},
    far_innov:   {v:3.0,  u:"FAR",         src:"indicative"},
    far_comm:    {v:3.5,  u:"FAR",         src:"indicative"},
    far_resi:    {v:2.5,  u:"FAR",         src:"UP TCP mixed-use (indicative)"},
    far_civic:   {v:1.8,  u:"FAR",         src:"indicative"},
    efficiency:  {v:0.80, u:"carpet/GFA",  src:"typical net-to-gross"},
    // capacity
    resi_area_pp:{v:30,   u:"m² carpet/person", src:"NBC 2016 / affordable-housing norm"},
    office_area_job:{v:12, u:"m²/job",     src:"typical commercial density"},
    lab_area_job:{v:22,   u:"m²/job",      src:"R&D density (indicative)"},
    dc_mw:       {v:20,   u:"MW IT load",  src:"RFP: 20 MW GPU/data-centre"},
    dc_jobs_mw:  {v:25,   u:"jobs/MW",     src:"data-centre O&M + ecosystem (indicative)"},
    dc_pue:      {v:1.30, u:"PUE",         src:"modern hyperscale target"},
    dc_wue:      {v:1.2,  u:"L/kWh",       src:"water-cooled DC (indicative)"},
    // water
    water_lpcd:  {v:135,  u:"LPCD",        src:"CPHEEO domestic norm"},
    water_nrw:   {v:0.15, u:"non-revenue frac", src:"target NRW"},
    sewage_frac: {v:0.80, u:"of water",    src:"CPHEEO"},
    recycle_frac:{v:0.50, u:"reuse frac",  src:"net-zero-water target"},
    // power (peak densities)
    kw_resi_pp:  {v:0.30, u:"kW/person",   src:"ECBC residential diversified"},
    kw_office_job:{v:0.14,u:"kW/job",      src:"ECBC commercial"},
    load_factor: {v:0.55, u:"annual LF",   src:"typical mixed load"},
    // solar
    solar_roof_frac:{v:0.55, u:"usable roof frac", src:"after setbacks/MEP"},
    solar_wp_m2: {v:0.15, u:"kWp/m²",      src:"rooftop PV density"},
    solar_yield: {v:1500, u:"kWh/kWp/yr",  src:"Lucknow GHI (NIWE/NREL)"},
    green_ppa_frac:{v:0.60,u:"green-power PPA frac", src:"open-access RE procurement"},
    // mobility
    trips_pp_day:{v:2.8,  u:"trips/person/day", src:"typical urban Indian"},
    transit_share:{v:0.50,u:"public+active frac", src:"TOD / net-zero-mobility target"},
    // capex (₹/m² GFA unless noted)
    cx_resi:     {v:32000,u:"₹/m²",        src:"CPWD plinth-area rates (indicative)"},
    cx_comm:     {v:48000,u:"₹/m²",        src:"grade-A commercial (indicative)"},
    cx_civic:    {v:40000,u:"₹/m²",        src:"public buildings"},
    cx_dc_cr_mw: {v:45,   u:"₹cr/MW",      src:"turnkey hyperscale DC (indicative)"},
    cx_infra_pp: {v:180000,u:"₹/person",   src:"trunk infra (roads/water/power/sewer) per capita"},
    cx_land_cr_ha:{v:6,   u:"₹cr/ha",      src:"UPAVP acquisition (indicative)"},
    // revenue (annual unless noted)
    rv_lease_m2: {v:9000, u:"₹/m² one-time", src:"saleable-space premium (booked over phasing)"},
    rv_tax_m2yr: {v:180,  u:"₹/m²/yr",     src:"property tax + service charge"},
    rv_dc_cr_mwyr:{v:9,   u:"₹cr/MW/yr",   src:"DC colo/lease + revenue-share (indicative)"},
    // finance
    debt_frac:   {v:0.70, u:"of capex",    src:"typical PPP gearing"},
    loan_rate:   {v:0.085,u:"p.a.",        src:"infra term-loan (indicative)"},
    loan_tenor:  {v:15,   u:"years",       src:"infra tenor"},
    dscr_target: {v:1.25, u:"min DSCR",    src:"lender covenant"},
    // carbon
    grid_ef:     {v:0.71, u:"tCO₂/MWh",    src:"CEA grid emission factor"},
    embodied_m2: {v:0.45, u:"tCO₂/m²",     src:"RICS/embodied-carbon (indicative)"},
  };
  const val = (ov, k) => (ov && ov[k] != null) ? ov[k] : A[k].v;

  // input: zoneAreaM2 = { ai, innov, comm, resi, civic, green } developable land (m²) by zone. ov = scenario overrides.
  function compute(zoneAreaM2, ov) {
    const z = Object.assign({ai:0,innov:0,comm:0,resi:0,civic:0,green:0}, zoneAreaM2 || {});
    const landM2 = Object.values(z).reduce((s,x)=>s+x,0), landHa = landM2/1e4;
    const eff = val(ov,"efficiency");
    // ---- 1. MASSING: gross floor area by zone (land × FAR), carpet = GFA × efficiency ----
    const gfa = { ai:z.ai*val(ov,"far_ai"), innov:z.innov*val(ov,"far_innov"), comm:z.comm*val(ov,"far_comm"),
                  resi:z.resi*val(ov,"far_resi"), civic:z.civic*val(ov,"far_civic") };
    const carp = k => gfa[k]*eff;
    const gfaTot = Object.values(gfa).reduce((s,x)=>s+x,0);
    // ---- 2. CAPACITY ----
    const dcMW = val(ov,"dc_mw");
    const population = Math.round(carp("resi")/val(ov,"resi_area_pp"));
    const jobs = Math.round(carp("comm")/val(ov,"office_area_job") + carp("innov")/val(ov,"lab_area_job")
                 + carp("civic")/25 + dcMW*val(ov,"dc_jobs_mw"));
    const dayPop = Math.round(population*0.6 + jobs);   // daytime presence
    // ---- 3. WATER + SEWAGE ----
    const dcWaterMLD = dcMW*1000*val(ov,"dc_pue")*24*val(ov,"dc_wue")/1e6;   // MW→kW→kWh/day→L→ML
    const domWaterMLD = population*val(ov,"water_lpcd")/1e6;
    const commWaterMLD = jobs*45/1e6;                                        // 45 L/job/day
    const waterMLD = (domWaterMLD+commWaterMLD+dcWaterMLD)/(1-val(ov,"water_nrw"));
    const sewageMLD = (domWaterMLD+commWaterMLD)*val(ov,"sewage_frac");
    const recycleMLD = sewageMLD*val(ov,"recycle_frac");
    const netWaterMLD = waterMLD - recycleMLD;
    // ---- 4. POWER ----
    const dcLoadMW = dcMW*val(ov,"dc_pue");
    const cityLoadMW = population*val(ov,"kw_resi_pp")/1000 + jobs*val(ov,"kw_office_job")/1000;
    const peakMW = dcLoadMW + cityLoadMW;
    const annualGWh = peakMW*8760*val(ov,"load_factor")/1000;
    // rooftop PV: usable roof ≈ ground-coverage of the built cells (a proxy), × usable fraction × PV density
    const roofM2 = (z.ai*0.5+z.innov*0.55+z.comm*0.5+z.resi*0.5+z.civic*0.45);
    const solarKWp = roofM2*val(ov,"solar_roof_frac")*val(ov,"solar_wp_m2");
    const solarGWh = solarKWp*val(ov,"solar_yield")/1e6;
    const ppaGWh = (annualGWh - solarGWh)*val(ov,"green_ppa_frac");
    const renewGWh = solarGWh + ppaGWh;
    const renewPct = annualGWh>0 ? renewGWh/annualGWh : 0;
    // ---- 5. MOBILITY ----
    const tripsDay = Math.round((population+jobs)*val(ov,"trips_pp_day"));
    const transitShare = val(ov,"transit_share");
    const transitTrips = Math.round(tripsDay*transitShare);
    const carTrips = tripsDay-transitTrips;
    // ---- 6. NET-ZERO CARBON ----
    const opEmissions = (annualGWh-renewGWh)*val(ov,"grid_ef")*1000;         // tCO₂/yr (residual grid)
    const embodied = gfaTot*val(ov,"embodied_m2");                          // one-time tCO₂
    // ---- 7. CAPEX (₹cr) ----
    const cx = {
      buildings: (carp("resi")*val(ov,"cx_resi") + (carp("comm")+carp("innov"))*val(ov,"cx_comm") + carp("civic")*val(ov,"cx_civic"))/1e7,
      datacentre: dcMW*val(ov,"cx_dc_cr_mw"),
      infra: population*val(ov,"cx_infra_pp")/1e7,
      land: landHa*val(ov,"cx_land_cr_ha"),
    };
    cx.total = cx.buildings+cx.datacentre+cx.infra+cx.land;
    // ---- 8. REVENUE (₹cr/yr; lease booked levelised over tenor) ----
    const tenor = val(ov,"loan_tenor");
    const leaseTotal = gfaTot*eff*val(ov,"rv_lease_m2")/1e7;
    const rv = {
      lease_annualised: leaseTotal/tenor,
      recurring: (gfaTot*eff*val(ov,"rv_tax_m2yr"))/1e7,
      datacentre: dcMW*val(ov,"rv_dc_cr_mwyr"),
    };
    rv.total = rv.lease_annualised+rv.recurring+rv.datacentre;
    // ---- 9. FINANCE ----
    const debt = cx.total*val(ov,"debt_frac"), r=val(ov,"loan_rate");
    const debtService = debt * r / (1-Math.pow(1+r,-tenor));               // level annuity ₹cr/yr
    const opexFrac = 0.35;                                                  // O&M as frac of recurring+dc revenue
    const noi = rv.total - (rv.recurring+rv.datacentre)*opexFrac;           // net operating income
    const dscr = debtService>0 ? noi/debtService : null;
    const payback = rv.total>0 ? cx.total/rv.total : null;                  // simple, years
    // rough project IRR: NOI perpetuity-ish vs capex
    const irr = cx.total>0 ? (noi/cx.total) : null;
    return {
      land:{ha:+landHa.toFixed(1), gfa_m2:Math.round(gfaTot)},
      capacity:{population, jobs, dayPop, dc_mw:dcMW},
      water:{demand_MLD:+waterMLD.toFixed(1), dc_MLD:+dcWaterMLD.toFixed(1), sewage_MLD:+sewageMLD.toFixed(1),
             recycled_MLD:+recycleMLD.toFixed(1), net_freshwater_MLD:+netWaterMLD.toFixed(1)},
      power:{peak_MW:+peakMW.toFixed(1), dc_MW:+dcLoadMW.toFixed(1), annual_GWh:+annualGWh.toFixed(0),
             solar_GWh:+solarGWh.toFixed(0), renewable_pct:+(renewPct*100).toFixed(0)},
      mobility:{trips_day:tripsDay, transit_trips:transitTrips, car_trips:carTrips, transit_share_pct:+(transitShare*100).toFixed(0)},
      carbon:{operational_tpa:Math.round(opEmissions), embodied_t:Math.round(embodied),
              net_zero_gap_pct:+((1-renewPct)*100).toFixed(0)},
      capex_cr:{buildings:+cx.buildings.toFixed(0), datacentre:+cx.datacentre.toFixed(0), infra:+cx.infra.toFixed(0),
                land:+cx.land.toFixed(0), total:+cx.total.toFixed(0)},
      revenue_cr:{lease_annualised:+rv.lease_annualised.toFixed(0), recurring:+rv.recurring.toFixed(0),
                  datacentre:+rv.datacentre.toFixed(0), total:+rv.total.toFixed(0)},
      finance:{debt_cr:+debt.toFixed(0), debt_service_cr:+debtService.toFixed(0), noi_cr:+noi.toFixed(0),
               dscr:dscr?+dscr.toFixed(2):null, payback_yrs:payback?+payback.toFixed(1):null,
               irr_pct:irr?+(irr*100).toFixed(1):null,
               verdict: dscr==null?"—":(dscr>=val(ov,"dscr_target")?"bankable":"viability gap")},
    };
  }
  root.MasterplanModel = { compute, ASSUMPTIONS:A };
})(typeof window!=="undefined" ? window : this);
