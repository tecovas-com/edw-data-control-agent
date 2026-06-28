# edw-data-control-agent

Alerting + recovery agent for the data platform. It is a **consumer** of
[`edw-data-control-center`](../edw-data-control-center): it polls that service's
freshness API and hands each stale model to an LLM agent that diagnoses, acts
within policy (re-running loaders when warranted), and alerts a human.

This repo never imports the control center's core — it talks to it over the
network with a GCP IAM ID token. See `CLAUDE.md` for architecture and conventions.

## Local Development Setup

### Install dependencies

```bash
pip install -r requirements-dev.txt
pytest -q
```

### Enviroonmennt vvariables
Copy the env template and fill in your secrets (Slack tokens, control-center URL):

```bash
cp .env.template .env
# then edit .env with your values
```

### Add SA key
- Go to 1Pass and find SA key for edw-data-control-agent
- Copy paste that into `gcp/sa_key.json`

### Test local Setup
- Run `python tests/test_dev_env.py` to check if dev setup is correct  

## Tooling

Shared, reusable tool clients live in `src/`; agents wrap their methods as
ADK tools. Each is constructed at the edge (`agent.py` / `main.py`) with an
injected I/O client so tests stub it (see `tests/stubs.py`).

### BigQuery (`src/bigquery.py`)

Read-only access to the warehouse, behind hard guardrails. Three tools:

| Tool | What it does |
|---|---|
| `list_tables(dataset)` | tables/views in an allowlisted dataset |
| `get_schema(dataset, table)` | columns of one table — incl. nested STRUCT/ARRAY paths and dbt-persisted descriptions (`COLUMN_FIELD_PATHS`) |
| `run_query(sql, *, caller=None)` | run a single read-only `SELECT`; returns rows + metadata |

**Guardrails** (the app-layer belt; the SA's IAM/ACL grants are the real
boundary — see `scripts/provision_bq_readonly_sa.py`):

- `is_read_only` rejects anything but a single `SELECT`/`WITH` (fast, friendly).
- a **dry-run** gates on `statement_type == "SELECT"` (blocks `EXPORT DATA` /
  DML / `CALL` / multi-statement) and on the **byte estimate** vs.
  `BQ_MAX_BYTES_BILLED` before the real run.
- referenced datasets must be in the allowlist (∪ authorized-source datasets,
  for raw read *through* authorized views like `dbt_views` → `raw_wfx`).
- the real job carries `maximum_bytes_billed`, query-cache, a pinned location,
  and `{agent, caller}` labels; on timeout the job is **cancelled**.
- results are capped by **row count and serialized bytes** (`truncated` flags it).

**Config** — read from the environment in `settings.py` (no code defaults); the
values below ship in `.env.template`, copy it to `.env` for local dev:

| Env var | `.env.template` value | Meaning |
|---|---|---|
| `BQ_PROJECT` | `tecovas-prod-edw` | query/billing project |
| `BQ_ALLOWED_DATASETS` | `core,base,dbt_views` | the dataset allowlist |
| `BQ_AUTHORIZED_SOURCE_DATASETS` | `raw_wfx` | raw datasets reachable via authorized views |
| `BQ_MAX_BYTES_BILLED` | 20 GiB | per-query scan cap (job fails if exceeded) |
| `BQ_MAX_ROWS` | 1000 | max rows returned |
| `BQ_MAX_RESULT_BYTES` | 256 KiB | max serialized result returned to the model |
| `BQ_QUERY_TIMEOUT_S` | 60 | client wait before cancelling the job |
| `BQ_LOCATION` | `US` | dataset location |

**Audit log** — every call emits one structured record via the injected
`audit_log` callback (default: one JSON line to stdout → Cloud Run logs):
`timestamp, agent, caller, label, sql, job_id, statement_type, bytes_processed,
bytes_billed, total_rows, returned_rows, truncated, duration_ms, status, error`.

> The read-only grants for the SA are provisioned by
> `scripts/provision_bq_readonly_sa.py` (`--dry-run` / `--verify` / `--teardown`).

## IAM Permissions

The agent runs as the **`edw-data-control-agent`** service account. The steps below
create it, hand you a local key, and grant the four things the agent needs:
call the model (Vertex AI), reach the control center (Cloud Run + IAP), and mint
its own ID token (sign-as-self). All commands target the `tecovas-prod-edw`
project; you need Owner/Editor (or the matching admin roles) to run them.

### 1. Create the service account
```bash
# Creates the identity the agent authenticates as. Run once per project.
gcloud iam service-accounts create edw-data-control-agent \
    --project tecovas-prod-edw \
    --display-name="EDW data control agent"
```

### 2. Get a local SA JSON key
```bash
# Downloads a key file used for local dev (referenced by GOOGLE_APPLICATION_CREDENTIALS).
# Treat as a secret — it's gitignored. In prod, prefer the attached SA (step 6) over a key file.
gcloud iam service-accounts keys create gcp/sa_key.json \
    --iam-account=edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com \
    --project tecovas-prod-edw
```

### 3. Grant Vertex AI access (call the LLM)
```bash
# Lets the SA invoke models (Gemini and Claude) on Vertex AI — includes
# aiplatform.endpoints.predict. Without this, model calls fail with 403 PERMISSION_DENIED.
# Note: IAM changes can take a couple minutes to propagate before predict succeeds.
gcloud projects add-iam-policy-binding tecovas-prod-edw \
    --member="serviceAccount:edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"
```

### 4. Grant Cloud Run invoker (reach the control center)
```bash
# Allows the SA to invoke the control-center Cloud Run service (the freshness API).
gcloud run services add-iam-policy-binding edw-data-control-center \
    --project tecovas-prod-edw \
    --region us-central1 \
    --member="serviceAccount:edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com" \
    --role="roles/run.invoker"
```

### 5. Grant IAP access (the control center sits behind IAP)
```bash
# Lets the SA pass through Identity-Aware Proxy in front of the control center.
# --condition=none applies the binding unconditionally (no IAM condition expression).
gcloud iap web add-iam-policy-binding \
    --resource-type=cloud-run \
    --service=edw-data-control-center \
    --region=us-central1 \
    --project=tecovas-prod-edw \
    --member="serviceAccount:edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com" \
    --role="roles/iap.httpsResourceAccessor" \
    --condition=none
```

```bash
# (Verify) Print the IAP policy to confirm the binding above landed.
gcloud iap web get-iam-policy \
    --resource-type=cloud-run \
    --service=edw-data-control-center \
    --region=us-central1 \
    --project=tecovas-prod-edw
```

### 6. Production: attach the SA + allow self-signed JWTs
Only needed when deploying the agent to its own Cloud Run service (not for local dev).
```bash
# Attach the SA so it's the ambient identity of the agent's Cloud Run service
# (no key file needed in prod — the runtime uses this identity directly).
gcloud run services update edw-data-control-agent \
    --service-account=edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com \
    --region=us-central1 --project=tecovas-prod-edw
```

```bash
# Let the SA sign JWTs as itself (auth.py mints an IAP ID token via signJwt).
# signJwt requires Token Creator on the *target* SA — here the SA grants it to itself.
gcloud iam service-accounts add-iam-policy-binding \
    edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com \
    --member="serviceAccount:edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountTokenCreator" --project=tecovas-prod-edw
```

### 7. Production: let the scheduler invoke the agent's own service
The Cloud Scheduler heartbeat POSTs to the agent's private `/run` endpoint,
authenticating **as the agent SA**. Cloud Run treats the *caller* and the
service's *runtime identity* as unrelated, so the SA must be granted
`run.invoker` on the agent service explicitly — being the service's attached SA
does NOT imply permission to invoke it. **Without this, every heartbeat fails
with `403 PERMISSION_DENIED` and the agent never runs.**
```bash
gcloud run services add-iam-policy-binding edw-data-control-agent \
    --project=tecovas-prod-edw \
    --region=us-central1 \
    --member="serviceAccount:edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com" \
    --role="roles/run.invoker"
```