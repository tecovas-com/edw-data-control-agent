#!/usr/bin/env bash
# Build and deploy the recovery agent to Cloud Run, then wire a Cloud Scheduler
# heartbeat. Edit the vars, then: ./deploy.sh
set -euo pipefail

PROJECT="${PROJECT:-tecovas-prod}"
REGION="${REGION:-us-central1}"
SERVICE="edw-data-control-agent"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/data/${SERVICE}:latest"

# Dedicated service account for the agent (least privilege).
AGENT_SA="edw-control-agent@${PROJECT}.iam.gserviceaccount.com"

# The control center this agent calls (its Cloud Run URL).
CONTROL_CENTER_URL="${CONTROL_CENTER_URL:?set CONTROL_CENTER_URL}"

echo ">> Build & push image"
gcloud builds submit --tag "${IMAGE}" --project "${PROJECT}"

echo ">> Deploy to Cloud Run (no public access; IAM only)"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-account "${AGENT_SA}" \
  --no-allow-unauthenticated \
  --set-env-vars "CONTROL_CENTER_URL=${CONTROL_CENTER_URL}"

echo ">> Grant the agent SA permission to invoke the control center"
echo "   (run once, on the control-center service):"
echo "   gcloud run services add-iam-policy-binding edw-data-control-center \\"
echo "     --member=serviceAccount:${AGENT_SA} --role=roles/run.invoker --region=${REGION}"

echo ">> Create the heartbeat (every 15 min) — Scheduler invokes /run with an OIDC token"
AGENT_URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT}" --format='value(status.url)')"
gcloud scheduler jobs create http "${SERVICE}-heartbeat" \
  --project "${PROJECT}" \
  --location "${REGION}" \
  --schedule "*/15 * * * *" \
  --uri "${AGENT_URL}/run" \
  --http-method POST \
  --oidc-service-account-email "${AGENT_SA}" \
  --oidc-token-audience "${AGENT_URL}" \
  || echo "(scheduler job may already exist; use 'jobs update' to change it)"

echo ">> Done. Agent at ${AGENT_URL}"
