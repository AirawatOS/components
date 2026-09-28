# Promote Air Intelligence v0.2.0 → EC2 (production)

Runbook for promoting the SDK-built, dev-sandbox-proven `ds:airawatdeveloper:solution/air-intelligence` v0.2.0
to the live EC2 platform (`airos.airawat.org`). ADR-0106 (sandbox) → ADR-0107 (dev→prod promotion).

**Status:** code pushed to origin/main (components@d06e9e0, platform-spec@5cf4e69). Dev-sandbox deploy PROVEN
(airawat-local). **Not yet on EC2.**

Steps marked **[USER]** mutate production (docker build/push, container recreate) — run them yourself; the
assistant is gated from prod mutation and must not work around it. `ssh airos` = ec2-user@13.205.13.220
(~/.ssh/airos-ec2.pem). Compose: `~/airawatos/docker-compose.yml` (backup `.bak`; rollback = restore + `up -d`).

---

## HARD PREREQUISITE — #277: EC2 network on 0.3.0
The clean promotion needs the SDK/dev-governance/promotion machinery on the live network. Today it is ABSENT:
```
POST https://airos.airawat.org/network/v1/promote          → 404   (ADR-0107 not deployed)
POST https://airos.airawat.org/network/v1/components/dev-token → (kernel; not exposed)
```
`air` domain + `/v1/schema-requests` + `/v1/specs/search` ARE present. So **#277 must land first.** Verify after:
```
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://airos.airawat.org/network/v1/promote -d '{}'   # expect 401/422, NOT 404
```

### #277 — bake + deploy network 0.3.0  [USER]
Zero-regression bake pattern (as with the contracts deploy): `FROM <running network tag> + COPY main api.py`
ONLY if the running image's deps match; otherwise a full main build. **Caution (from prior attempts): the full
main api.py did NOT boot on the EC2 network image — `authn` lacked `set_trusted_issuers` (image drift).** So
either (a) reconcile that drift in a full 0.3.0 build, or (b) surgically append the missing endpoints
(dev-token proxy, /v1/promote, S2/S3 hooks) to the running tag like the specs-search patch. Confirm boot before
compose-bumping.
```
# after building/pushing manishsv/airawatos-network:0.3.0 (or a surgical tag):
ssh airos 'cd ~/airawatos && cp docker-compose.yml docker-compose.yml.bak \
  && sed -i "s#airawatos-network:[0-9.]*#airawatos-network:0.3.0#" docker-compose.yml \
  && docker compose pull network && docker compose up -d network \
  && docker exec airawat-network python -c "import api" && echo BOOT-OK'
```

---

## PROMOTION (after #277 is live)

Air Intelligence's `air` domain + air.* schemas already exist on EC2 (v0.1.0). Promotion re-governs the NET-NEW
vocab, re-certifies the re-homed SDK components under prod authority, publishes, and installs into a production
tenant (**newdelhi** — it holds the real PM2.5 / station data the loop needs).

### 1. Point the aos CLI at EC2 and dry-run  [assistant can prepare / run read-only]
Run from a host that can reach the EC2 network+kernel+identity (or inside an EC2 container on the compose net,
mirroring the local deploy). Env:
```
export AIRAWAT_NETWORK=https://airos.airawat.org/network
export AIRAWAT_KERNEL=<ec2 kernel url>          # internal — confirm after #277
export AIRAWAT_IDENTITY=<ec2 identity url>      # EC2 login is {username,tenant} (NOT the local {username})
export AIRAWAT_DEV_USER=<developer principal>
```
Offline structural check first (no services): `aos deploy --sandbox solutions/air-intelligence`.

### 2. Promote  [USER runs the mutation; assistant prepares the exact call]
Once `/v1/promote` exists:
```
aos promote air-intelligence --to production --tenant newdelhi
```
`aos promote` (ADR-0107): canonicalize dev-scoped vocab (`ds:airawatdeveloper:vocab/air.field_report` → `air.field_report`)
→ standards-body re-govern (real request→decide, NOT dev auto-approve) → re-certify the 8 members under prod
authority → publish (store/service-offering; S3 now passes in prod context) → install the closure into `newdelhi`
→ signed promotion receipt. If `aos promote` is not yet wired, the manual equivalent is: govern the 2 new vocab
items (air.field_report schema + air.recommend verb-interface) as steward → `/v1/certify` each member (prod) →
publish → `/v1/solutions` + install into newdelhi → seed the 2 subscriptions + 3 viz layers.

### 3. Deliver components + app bundle  [USER]
Components are filestore-delivered (ADR-0093) — the install materializes them. If `air-explorer` (re-homed to a
new id) needs a fresh app bundle, publish it (Studio publish or app-host overlay) and recreate app-host:
```
ssh airos 'cd ~/airawatos && docker compose up -d app-host'
```

### 4. Seed data + verify the loop
- Ensure `newdelhi` has an AOI + air.sensor_reading (run `air-sensors` / `air-lowcost-sensors`).
- Run station-interpolation → hotspot-detection; a hotspot fires `air.recommend` → recommendation → `decision.route`
  → an Inbox action for `air.enforcement-officer`. File an `air-field-report` (photo via ctx.file) to close it.
- Verify in Solution Studio (EC2): the solution + members appear; badge NOT "Under Development" (prod edition).

---

## Rollback
Prod install is reversible via `aos destroy --live` (unseed subs/viz → uninstall grants → decertify → retract new
vocab). Network image: restore `docker-compose.yml.bak` + `docker compose up -d network`.
