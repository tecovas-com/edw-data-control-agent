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