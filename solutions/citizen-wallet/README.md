# Citizen wallet + DigiLocker (sandbox)

One citizen account across every department of the network:

- **Verified identity**: simulated UIDAI enrolment + eKYC → `credential.identity_verified` (Jan Seva → My account).
- **Linked department records** (account aggregation): a department matches the verified name against its own
  record (e.g. `urban.property_holding`) and issues a signed credential (`credential.urban_property_holding`).
- **Wallet**: network-issued credentials the citizen accepts into their own tenant; presented to any department
  as verified evidence (holder-held, issuer-signed).
- **DigiLocker adapter — SANDBOX**: the `digilocker-sandbox` tenant holds `identity.issued_document` records
  (fictional documents from "(sandbox)" issuers). *Fetch* issues `credential.digilocker_document` into the wallet;
  *Save to DigiLocker* pushes an Airawat-issued credential into the locker. The real adapter (DigiLocker
  Requester/Issuer partner APIs, OAuth consent) replaces the sandbox calls behind the same bridge
  (`/bridge/digilocker` status | list | fetch | push).

| File | What |
|---|---|
| `govern_wallet.py` | registers the sandbox tenant; governs `identity.issued_document` (sensitive) + `credential.digilocker_document` |
| `seed_sandbox.py` | the demo stories' locker documents (all fictional) — runs in the staging app-host as the sandbox principal |

Staging host side: platform-spec `chore/legacy-staging-demo-ops`, patch 9 (`patches/2026-11-17-citizen-wallet.py`).
