# Urban services (RajUUP as the reference)

The product: urban-sector services — property, land, building, licences and civic services — for every urban
department of a state, built on AirawatOS. Each department is a tenant; all of them are members of one network, so a
citizen sees one portal and one chat assistant across them. Services are configuration (Service Studio), not code.

| File | What |
|---|---|
| `govern_urban_schemas.py` | The shared master-data vocabulary (domain `urban`): `urban.property` (Unified Property ID), `urban.property_holding`*, `urban.scheme`, `urban.plot`, `urban.allotment`*, `urban.lease`*, `urban.fee_schedule`, `urban.document_type`. (* sensitive — personal data kept out of the core records.) The GIS parcel is the existing `geo.parcel`. |
| `seed_registries.py` / `.sh` | Deterministic, fictional master data for the seven demo departments (~7,400 records), written by each department's own admin through the kernel. |

Demo tenants (staging): `urban-net` (the network), `state-udd`, `dlb`, `housing-board`, `sidc`, `city-da`, `uit`,
`city-mc`. Provisioning, offices and roles: platform-spec `chore/legacy-staging-demo-ops`, `operator/legacy-staging/urban-demo/`.
