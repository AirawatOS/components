# Public Expenditure Dashboard — governed data model (Janaagraha)

The extractor emits ONE wide, denormalized row per work. On AirawatOS that becomes a small set of **governed
nouns** (dotted `domain.subject`, each with its own id), linked by reference — so a road, its works, the tender,
the contract, the contractor, the payment and the documents are each addressable, reusable and independently
sourced. The extractor's per-field *method / confidence / note* columns are **not** modelled as bespoke
columns — they map to the platform's lineage+confidence pattern: a `prov` object + a `confidence` number on
each record (ADR-0041).

## The nouns (reuse first)

| Noun | Status | Role |
|---|---|---|
| `geo.ward` | **exists — reuse** | ward boundary (ward_no/ward_name/zone resolve to this) |
| `geo.osm_feature` | **exists — reuse** | the raw OSM road geometry a segment is matched to |
| `knowledge.document` | **exists — reuse** | every supporting PDF (BOQ, completion cert, agreement, tender notice, bill, measurement book, QC) + citation/provenance |
| `finance.transaction` | exists | the double-entry ledger a payment can post to (optional) |
| `geo.road` | **NEW** | the named street a citizen picks — the anchor |
| `geo.road_segment` | **NEW** | a surveyed segment of a road (start/end, length) — the unit a work touches |
| `works.work_order` | **NEW** | the core "road work": sanction, type, roads touched, completion |
| `procurement.tender` | **NEW** | the tender that preceded the work |
| `works.contract` | **NEW** | the awarded contract: value, agreement, defect-liability period |
| `works.contractor` | **NEW** | the contractor (party) |
| `works.payment` | **NEW** | what was actually paid vs the contract |

> `tender` lands in the existing `procurement.*` domain; `payment` is a `works.payment` (consistent with the
> `tax.payment` / `justice.payment` precedent) and can post to `finance.transaction` when you want ledgered money.
> The `n_docs_*` counts are a **derived rollup** over `knowledge.document` (a view), not stored columns.

## New schema definitions (governance-ready JSON Schema)

```yaml
geo.road:                     # x-kind: entity — the citizen's anchor
  required: [schema, id, road_id, street_name]
  properties:
    id: {type: string, pattern: "^ds:[a-z0-9-]+:\\S+$"}
    schema: {const: geo.road}
    road_id: {type: string}              # street_id
    street_name: {type: string}
    ward: {type: [string, null]}         # -> geo.ward id (ward_no/ward_name)
    zone: {type: [string, null]}
    geometry: {type: [object, null]}     # merged line of its segments
    osm_feature: {type: [string, null]}  # -> geo.osm_feature id
    tenant: {type: [string, null]}
  additionalProperties: false

geo.road_segment:             # x-kind: entity — the surveyed unit a work touches
  required: [schema, id, segment_id, road]
  properties:
    id: {pattern: "^ds:[a-z0-9-]+:\\S+$"}
    schema: {const: geo.road_segment}
    segment_id: {type: string}           # street_id + segment_no
    road: {type: string}                 # -> geo.road id
    segment_no: {type: [integer, null]}
    sheet_index: {type: [integer, null]}
    start_point: {type: [object, null]}  # {lat, lon}  (start_lat/start_lon)
    end_point: {type: [object, null]}    # {lat, lon}  (end_lat/end_lon)
    length_m: {type: [number, null]}
    from_landmark: {type: [string, null]}
    to_landmark: {type: [string, null]}
    geometry: {type: [object, null]}
    prov: {type: [object, null]}         # road_key_method, road_extraction_note
    confidence: {type: [number, null]}   # road_key_confidence (0..1)
    tenant: {type: [string, null]}
  additionalProperties: false

works.contractor:             # x-kind: entity — the party
  required: [schema, id, name]
  properties:
    id: {pattern: "^ds:[a-z0-9-]+:\\S+$"}
    schema: {const: works.contractor}
    name: {type: string}                 # contractor_name_road_history / contractor_on_agreement
    reg_no: {type: [string, null]}       # contractor_reg_no
    tenant: {type: [string, null]}
  additionalProperties: false

works.work_order:             # x-kind: entity — the core work
  required: [schema, id, work_order_no]
  properties:
    id: {pattern: "^ds:[a-z0-9-]+:\\S+$"}
    schema: {const: works.work_order}
    job_number: {type: [string, null]}
    work_order_no: {type: string}
    work_order_date: {type: [string, null]}
    work_type: {type: [string, null]}
    other_work: {type: [string, null]}
    work_name: {type: [string, null]}
    significant_landmark: {type: [string, null]}
    sanctioned_amount: {type: [number, null]}
    sanctioned_year: {type: [string, null]}
    extension_days: {type: [integer, null]}
    roads: {type: [array, null], items: {type: string}}   # -> geo.road / geo.road_segment ids (roads_this_work_touches)
    n_segments_mapped: {type: [integer, null]}
    mapped_len_m: {type: [number, null]}
    ward: {type: [string, null]}
    zone: {type: [string, null]}
    status: {enum: [sanctioned, awarded, in_progress, completed, null]}
    completed_on: {type: [string, null]}
    final_cost: {type: [number, null]}
    saving: {type: [number, null]}                        # sanctioned/contract - final
    completion_status: {type: [string, null]}             # completion_verdict
    completion_stated_figure: {type: [number, null]}
    prov: {type: [object, null]}                          # completion_method/note, road_extraction_note
    confidence: {type: [number, null]}                    # completion_confidence
    tenant: {type: [string, null]}
  additionalProperties: false

procurement.tender:           # x-kind: entity
  required: [schema, id, work_order]
  properties:
    id: {pattern: "^ds:[a-z0-9-]+:\\S+$"}
    schema: {const: procurement.tender}
    work_order: {type: string}           # -> works.work_order id
    tender_indent: {type: [string, null]}
    tender_number: {type: [string, null]}
    notification_numbers: {type: [array, null], items: {type: string}}
    estimated_value: {type: [number, null]}
    published: {type: [string, null]}
    status: {type: [string, null]}       # tender_status
    source_link: {type: [string, null]}  # Karnataka eProcurement URL
    prov: {type: [object, null]}         # tender_link_method
    confidence: {type: [number, null]}   # tender_link_confidence
    tenant: {type: [string, null]}
  additionalProperties: false

works.contract:               # x-kind: entity
  required: [schema, id, work_order]
  properties:
    id: {pattern: "^ds:[a-z0-9-]+:\\S+$"}
    schema: {const: works.contract}
    work_order: {type: string}           # -> works.work_order
    tender: {type: [string, null]}       # -> procurement.tender
    contractor: {type: [string, null]}   # -> works.contractor
    contract_value: {type: [number, null]}
    discount_pct: {type: [number, null]}
    method: {type: [string, null]}       # contract_method (e.g. L1 tender)
    agreement_date: {type: [string, null]}
    dlp_from: {type: [string, null]}     # defect-liability period
    dlp_to: {type: [string, null]}
    dlp_status: {enum: [active, expired, unknown, null]}
    prov: {type: [object, null]}         # contract_confidence_note
    confidence: {type: [number, null]}   # contract_confidence
    tenant: {type: [string, null]}
  additionalProperties: false

works.payment:                # x-kind: record — a financial fact
  required: [schema, id, work_order]
  properties:
    id: {pattern: "^ds:[a-z0-9-]+:\\S+$"}
    schema: {const: works.payment}
    work_order: {type: string}           # -> works.work_order
    contract: {type: [string, null]}     # -> works.contract
    paid_net: {type: [number, null]}
    paid_gross: {type: [number, null]}
    net_vs_contract: {type: [number, null]}
    payment_flag: {type: [string, null]} # e.g. overpaid / within-contract
    paid_on: {type: [string, null]}
    transaction: {type: [string, null]}  # -> finance.transaction (optional ledger post)
    prov: {type: [object, null]}
    confidence: {type: [number, null]}
    tenant: {type: [string, null]}
  additionalProperties: false
```

## Supporting documents — reuse `knowledge.document`

Each PDF the extractor references is one `knowledge.document` linked to the work, with a `doc_type`:
`road_history · completion_cert · agreement · boq · tender_notification · bill · measurement_book ·
quality_control`. The `n_documents_total` / `n_docs_*` numbers are a **count view** over these, not stored
fields; `n_documents_excluded_pii` is a provenance note on the work_order's `prov`.

## Every extractor field → where it lands

- **Road / segment:** street_id → `geo.road.road_id`; street_name → `geo.road.street_name`; ward_no/ward_name →
  `geo.ward` (ref); zone → `geo.road.zone`; segment_n/segment_no/sheet_index, start_lat/lon→`start_point`,
  end_lat/lon→`end_point`, length_m, from_landmark, to_landmark → `geo.road_segment`;
  road_key_method/road_key_confidence/road_extraction_note → `geo.road_segment.prov`/`.confidence`;
  roads_this_work_touches/n_segments_mapped/mapped_len_m → `works.work_order`.
- **Work:** job_number, work_order_no, work_order_date, work_type, other_work, work_name,
  significant_landmark, sanctioned_amount, sanctioned_year, extension_days → `works.work_order`.
- **Tender:** tender_indent, tender_number, tender_notification_numbers, tender_estimated_value,
  tender_published, tender_status, tender_link_method/confidence → `procurement.tender`.
- **Contract:** contract_value, discount_pct, agreement_date, contractor_on_agreement, contract_method,
  dlp_from/to/status, contract_confidence/_note → `works.contract`.
- **Contractor:** contractor_name_road_history, contractor_reg_no → `works.contractor`.
- **Payment:** paid_net, paid_gross, net_vs_contract, payment_flag → `works.payment`.
- **Completion:** completed_on, final_cost, saving, completion_verdict, completion_stated_figure,
  completion_note, completion_method, completion_confidence → `works.work_order` (completion_* →
  prov/confidence) + a `completion_cert` `knowledge.document`.
- **Documents:** n_documents_total / n_docs_road_history / n_docs_completion_cert / n_docs_agreement /
  n_docs_boq / n_docs_tender_notification / n_docs_bill / n_docs_measurement_book / n_docs_quality_control →
  count view over `knowledge.document`; n_documents_excluded_pii → work_order `prov`.
