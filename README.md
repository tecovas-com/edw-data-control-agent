# edw-data-control-agent

Alerting + recovery agent for the data platform. It is a **consumer** of
[`edw-data-control-center`](../edw-data-control-center): it polls that service's
freshness API, runs a deterministic recovery runbook for known failures, and
escalates ambiguous cases to an LLM agent that diagnoses, acts within policy,
and alerts a human.

This repo never imports the control center's core — it talks to it over the
network with a GCP IAM ID token. See `CLAUDE.md` for architecture and conventions.

## Quick start

```bash
pip install -r requirements-dev.txt
pytest -q

# point at the control center
export CONTROL_CENTER_URL="https://edw-data-control-center-xxxx.run.app"

# run one heartbeat locally (uses ambient GCP credentials)
python -m entrypoints.cron --once
```

## Local Development Setup

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

### Create Service Accountn
```
gcloud iam service-accounts create edw-data-control-agent \
    --project tecovas-prod-edw \
    --display-name="EDW data control agent"
```

### Get SA JSON key
```
gcloud iam service-accounts keys create gcp/sa_key.json \
    --iam-account=edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com \
    --project tecovas-prod-edw
```

### Grant SA Invoker permissions
This grants the agent's service account permission to invoke the control-center Cloud Run service
```
gcloud run services add-iam-policy-binding edw-data-control-center \
    --project tecovas-prod-edw \
    --region us-central1 \
    --member="serviceAccount:edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com" \
    --role="roles/run.invoker"
```

```
gcloud iap web add-iam-policy-binding \
    --resource-type=cloud-run \
    --service=edw-data-control-center \
    --region=us-central1 \
    --project=tecovas-prod-edw \
    --member="serviceAccount:edw-data-control-agent@tecovas-prod-edw.iam.gserviceaccount.com" \
    --role="roles/iap.httpsResourceAccessor" \
    --condition=none
```

```
gcloud iap web get-iam-policy \
    --resource-type=cloud-run \
    --service=edw-data-control-center \
    --region=us-central1 \
    --project=tecovas-prod-edw
```