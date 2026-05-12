# Install — Clinical Co-Pilot for OpenEMR

The Clinical Co-Pilot is two pieces that talk to each other over the FHIR (Fast Healthcare Interoperability Resources) API:

1. **This OpenEMR module** (PHP). Adds a launch button to every patient summary page and routes credentials to the sidecar.
2. **The sidecar** (Python + Postgres). The actual diagnostic engine. Runs in its own container next to OpenEMR or on a separate host.

You install both. Five minutes if your OpenEMR is already up.

## Four-line install (TL;DR)

```bash
# In your OpenEMR install root:
cd interface/modules/custom_modules
curl -L https://github.com/scott-lydon/openemr/releases/latest/download/oe-module-clinical-copilot.zip -o copilot.zip
unzip copilot.zip && rm copilot.zip
# Then: Admin → Modules → Register, Install, Enable. Open Settings, paste sidecar URL + license key, click "Generate" on the JWT key, mirror the value into the sidecar's env.
```

## Detailed install

### 1. Stand up the sidecar

```bash
# Pick a directory on a host with docker + docker compose installed.
mkdir clinical-copilot && cd clinical-copilot

# Download the self-host compose file.
curl -L https://raw.githubusercontent.com/scott-lydon/openemr/master/clinical-copilot/deploy/docker-compose.openemr-sidecar.yml \
  -o docker-compose.yml

# Generate the JWT signing key (mirror this into the OpenEMR module too).
openssl rand -hex 32 | awk '{print "COPILOT_BFF_JWT_SIGNING_KEY=" $0}' > .env

# Paste your license key (from the Stripe Checkout success page).
echo "COPILOT_LICENSE_KEY=cc_live_paste_here" >> .env

# Choose an LLM provider.
echo "COPILOT_LLM_PROVIDER=openai" >> .env
echo "OPENAI_API_KEY=sk-..." >> .env

# Boot it.
docker compose up -d
```

Verify with:

```bash
curl -sf http://localhost:8801/diagnostic | jq
```

You should see `"auth_method": "private_key_jwt"` and `"purpose_check_class": "membership_in_authorized_purposes"`. If you see anything else, you are running an old build — check `running_git_hash` against the latest release.

### 2. Install the OpenEMR module

```bash
# Inside your OpenEMR install:
cd interface/modules/custom_modules
curl -L https://github.com/scott-lydon/openemr/releases/latest/download/oe-module-clinical-copilot.zip -o copilot.zip
unzip copilot.zip
rm copilot.zip
```

Open OpenEMR in your browser:

1. Click `Admin → Modules → Manage Modules`.
2. Click the `Unregistered` tab. Find `Clinical Co-Pilot`. Click `Register`.
3. Click the `Registered` tab. On the Clinical Co-Pilot row, click `Install` then `Enable`.

### 3. Configure the module

1. Open the patient summary page for any patient. You will see a `Clinical Co-Pilot (AI)` button at the top. Clicking it now will give an error because the module is not configured yet. That is expected.
2. Click the gear icon next to Clinical Co-Pilot in the Modules list. The admin settings page opens.
3. Fill in:
   - **Sidecar URL**: the URL where you stood up the sidecar in step 1 (for example, `http://localhost:8801` for a local-host pair, or `https://copilot.your-clinic.com` for a hosted sidecar).
   - **License Key**: paste the value you put into the sidecar's `.env` for `COPILOT_LICENSE_KEY`.
   - **JWT Signing Key**: click `Generate New Key`. Copy the new value out of the green banner.
4. SSH into the sidecar host and replace the `COPILOT_BFF_JWT_SIGNING_KEY` value in `.env` with the new one you just generated, then `docker compose restart copilot-sidecar`.
5. Back in OpenEMR, click `Test Connectivity`. You should see `Sidecar reachable. Version: <sha>. Auth method: private_key_jwt`.

### 4. Smoke test

1. Open any patient with at least one Condition, one MedicationRequest, and one Observation in their chart.
2. Click `Clinical Co-Pilot (AI)`. A new tab opens.
3. Type `What are the top 3 most likely diagnoses to rule out given this chart?` and hit send.
4. Within ~10 seconds you should see 3 ranked diagnoses, each with a citation that links back to a FHIR resource in the patient's chart.

If anything fails: the OpenEMR error log at `Admin → System → Backup → View Logs` records every step of the launch flow with a `clinical_copilot.launch.*` prefix; the sidecar's log (`docker logs copilot-sidecar`) records every `/chat` call.

## Uninstall

1. `Admin → Modules → Manage Modules → Registered`. On the Clinical Co-Pilot row click `Disable`, then `Uninstall`. This drops the module's private settings table and disables the OAuth client (the client is left in place for audit purposes; delete it manually from `Admin → API Clients` if you want it gone).
2. Delete the `oe-module-clinical-copilot/` directory.
3. (On the sidecar host) `docker compose down -v` to remove the sidecar containers and Postgres volume.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Test Connectivity` returns HTTP 0 / curl error | Verify the URL is reachable from the OpenEMR host: `curl -fv <sidecar-url>/diagnostic`. Usually a Docker network issue. |
| `Test Connectivity` returns HTTP 200 but `purpose_check_class` is `strict_equality_legacy` | You are running an old sidecar image. `docker compose pull && docker compose up -d`. |
| Launch button says `HTTP 503: signing key not configured` | You did not click `Generate New Key` on the admin page, or you forgot to mirror the value into the sidecar's `.env`. |
| `/chat` returns HTTP 402 | License key missing/expired. Check `/diagnostic` — the `license_state` field tells you which. Renew at `https://copilot.scott-lydon.dev/billing`. |
| Launch button does not appear at all | Either the module is not installed/enabled, or your sidecar URL is empty in the module admin page. |

## Support

- Bug reports: https://github.com/scott-lydon/openemr/issues
- Email: relays.inanity.0n@icloud.com (rotate as the product matures)
- Forum: https://community.open-emr.org under "Custom Modules"
