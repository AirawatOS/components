# The Avsar Worker Passport — Aadhaar → job → insurance → benefits

How an informal/gig worker builds a portable, cryptographically‑verifiable career passport across India's
identity, skilling, labour, transport and welfare systems — **without any of them merging their databases.**

> **One idea:** every authority the worker touches *signs a credential into one wallet* (keyed to a single
> portable identity, the `sub`); every employer and agency *verifies from that wallet*. The worker is the
> integration point — no central database, and a check one party pays for is reusable proof for all the others.

Visual reference (shareable): the published artifact "The Avsar Worker Passport".

---

## The journey, step by step

Each step either **issues** a signed credential into the wallet, or **verifies** credentials already in it.

| # | Stage | Actor | Issue / Verify | Credential |
|---|---|---|---|---|
| 0 | **Aadhaar enrolment** (prerequisite, one‑time) | UIDAI kiosk | issue | Aadhaar number + UIDAI enrolment record (`identity.aadhaar`) |
| 1 | **Register into Avsar** | worker (self‑service) | — | Avsar account (`sub`), empty wallet, unverified |
| 2 | **eKYC** — verify the Aadhaar | UIDAI / licensed KUA | issue | `credential.identity_verified` (account activated) |
| 3 | **Get skilled & registered** | ITI · eShram · RTO · Police | issue | `skill_certificate`, `eshram_worker`, `driving_license`, `police_verification` |
| 4 | **Get a job** (onboard with a platform) | Ola — verifier, then issuer | verify + issue | verifies identity+licence+police → issues `work_experience`, `proof_of_work`, `proof_of_payment` |
| 5 | **Get protected** (insurance) | ESIC | verify + issue | verifies eshram+work → issues `insurance_enrolled` |
| 6 | **Get a better job** | NCS | verify + issue | verifies skill+experience → issues `job_placement` |
| 7 | **Portability** | Swiggy / next employer / lender | verify | re‑presents any credential — **no re‑verification** |

## Credential catalogue

| Credential | Issued by (tenant) | Verified by | Unlocks |
|---|---|---|---|
| `credential.identity_verified` | UIDAI (`uidai`) | everyone | an activated, trusted identity |
| `credential.skill_certificate` | ITI (`iti`) | employers, NCS | first job, higher wage band |
| `credential.eshram_worker` | eShram / Labour (`labour`) | ESIC, schemes | government worker benefits |
| `credential.driving_license` | RTO (`parivahan`) | gig platforms | driver onboarding |
| `credential.police_verification` | State Police (`police`) | gig platforms | passenger‑facing work |
| `credential.work_experience` | Ola/Swiggy (`ola`/`swiggy`) | next employer, lenders | credit, the next job |
| `credential.proof_of_work` / `credential.proof_of_payment` | Ola/Swiggy | lenders, insurers | labour disputes, loans, eligibility |
| `credential.insurance_enrolled` | ESIC (`esic`) | the worker / claims | a safety net |
| `credential.job_placement` | NCS (`ncs`) | the worker | a better role |

## Why it holds — trust mechanics

- **Issuer signature** — each credential is Ed25519‑signed with the issuing tenant's Vault steward key; verifiable offline against the published key.
- **Holder signature + consent** — to present, the worker signs over the verifier's fresh challenge and consents; two signatures travel (issuer + holder).
- **Holder binding** — every credential is bound to the one `sub`; a verifier checks presentation and credential name the same holder.
- **No central honeypot** — each agency keeps its own data and issues claims; the consented wallet is the only integration surface.

## Why it gets adopted (three sides)

- **Worker** — one sign‑in; a portable record (jobs, credit, insurance, benefits) they own and carry.
- **Platform** — driver onboarding in minutes not days, no fake accounts, verification reused across the industry — a direct cost/fraud win. **The real adoption engine** (as telcos were for Aadhaar).
- **Government** — dormant eShram records become activated+verified; the 14 benefit programmes reach workers where they already are (the platforms do last‑mile distribution).

## Honest boundaries

Live reference on AirawatOS. **Real:** governed credential types, each authority a tenant with its own
signing key, genuinely signed credentials, real signature + holder verification, consent records, cross‑origin
embedding. **Stood in for:** Aadhaar enrolment + the eKYC OTP are simulated (production plugs a licensed
AUA/KUA into the IdP slot + real biometric capture; enrolment/issuer‑admin endpoints would be operator‑gated).

## Run the demo

```bash
# backend chain (idempotent): governs schemas, creates issuer tenants, issues the full passport to one worker
scp run.py ec2-user@<host>:/tmp/passport.py
ssh ec2-user@<host> 'docker cp /tmp/passport.py airawat-app-host:/tmp/passport.py'
ssh ec2-user@<host> 'docker exec -e IDENTITY_INTERNAL=$(docker exec airawat-identity printenv IDENTITY_INTERNAL 2>/dev/null || echo airawat-identity-internal) airawat-app-host python3 /tmp/passport.py'

# the worker-facing pages (genuinely separate origin from the backend)
cd components/demos/worker-career-passport && python3 -m http.server 8777
#   http://localhost:8777/uidai-enrol.html   → enrol (get an Aadhaar number)
#   http://localhost:8777/ola.html           → Register into Avsar → eKYC → Drive with Ola (onboard) → Benefits
```

Backend endpoints: `/embed/uidai-enrol`, `/register`, `/embed/signup`, `/embed/ekyc-issue`, `/embed/services`,
`/embed/request`, `/embed/proof-request` + `/embed/present` + `/embed/verify-presentation`, `/v1/credentials/issue` + `/verify`.
Live: `app-host 0.2.27-cred-values`.
