# Worker Career Passport (Avsar) — a live demo of the worker‑centric credential federation

A gig / unorganised worker opens **one** portable livelihood account — **Avsar** (अवसर, "opportunity") — and
every authority they touch **issues a signed Verifiable Credential** into that one wallet, keyed to the worker's
network identity (`sub`). The credentials compound into a verifiable **career passport** the worker carries
across platforms. No central database is merged — the worker is the integration point.

    Register into Avsar → GET SKILLED → GET A JOB → GET PAID → GET PROTECTED → GET A BETTER JOB → (loop)

## The cast (each a real network tenant with its own Vault signing key)

| Tenant | Role | Issues |
|---|---|---|
| `uidai`  | eKYC / identity assurance (licensed KUA) | `credential.identity_verified` |
| `iti`    | Industrial Training Institute (skilling) | `credential.skill_certificate` |
| `labour` | Ministry of Labour & Employment — **eShram** | `credential.eshram_worker` |
| `ola`    | gig platform (employer) | `credential.work_experience` (90‑day), `proof_of_work`, `proof_of_payment` |
| `swiggy` | gig platform (employer) | `credential.work_experience` (general) — portability |
| `esic`   | worker insurance | `credential.insurance_enrolled` |
| `ncs`    | National Career Service | `credential.job_placement` — the "better job" |

## What it proves (ADR‑0112 / ADR‑0049 / ADR‑0067)

- **One portable identity.** The `sub` is the single join key; sign in once, used by every agency.
- **Issue once, reuse everywhere.** ESIC and NCS *verify* the ITI / Ola credentials (issuer signature +
  holder‑binding) instead of re‑doing KYC or re‑collecting documents.
- **Gig work becomes verifiable history.** Ola/Swiggy issue experience + payment proofs — turning work that's
  invisible to banks and employers today into portable proof (credit, insurance, the next job).
- **No honeypot.** Each agency keeps its own data and issues claims; the worker's consented wallet is the
  integration surface, not a merged mega‑database.

## Run it (live, against the EC2 network)

The script talks to the network (`:8090`) + reference IdP (`:8093`) from inside the app‑host container.
`POST /users` on the IdP needs the internal provisioner token, supplied at runtime (never hardcoded):

```bash
scp run.py ec2-user@<host>:/tmp/passport.py
ssh ec2-user@<host> 'docker cp /tmp/passport.py airawat-app-host:/tmp/passport.py'
ssh ec2-user@<host> 'docker exec \
  -e IDENTITY_INTERNAL=$(docker exec airawat-identity printenv IDENTITY_INTERNAL 2>/dev/null || echo airawat-identity-internal) \
  airawat-app-host python3 /tmp/passport.py'
```

It is idempotent — re‑running re‑governs the same schemas/tenants harmlessly and re‑issues the passport.

## Steps (what the script does)

0. authenticate as the **airawat steward** (governance authority)
1. govern the **8 credential types** (`POST /v1/schemas`)
2. create the **7 issuer tenants** (`POST /v1/tenants` — each auto‑gets a Vault steward key)
3. provision one **admin login per tenant** (`POST /users`, internal token)
4. the worker **Registers into Avsar** (`POST /register` → portable `sub`)
5. issue the **career arc** — each authority signs a VC to the worker's `sub`; ESIC + NCS verify before acting
6. publish **benefit/job service offerings** (discoverable from an embedding app)
7. print the **Avsar wallet** — the full, independently‑verifiable passport

## Known caveats (reference deployment)

- Network governance records (schemas, tenants, offerings) are held in the reference network's in‑memory
  stores; a network restart may require a re‑run. Issued credentials + the Avsar account persist.
- `uidai` here stands in for a licensed Aadhaar eKYC provider (AUA/KUA); real deployment plugs a licensed
  KUA into the IdP slot (ADR‑0068). eKYC still requires the worker present for a one‑time step.
