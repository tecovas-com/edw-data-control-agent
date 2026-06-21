#!/usr/bin/env bash
# Build & deploy the recovery agent to Cloud Run, then wire a Cloud Scheduler
# heartbeat. All app config is read from ./.env, so just: ./deploy.sh
#
# ── ONE-TIME PREREQUISITES (run once per project) ────────────────────────────
#   PROJECT=tecovas-prod-edw
#   SA=edw-data-control-agent@${PROJECT}.iam.gserviceaccount.com
#
#   # 1) APIs + local Docker auth for Artifact Registry
#   gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
#       cloudscheduler.googleapis.com iamcredentials.googleapis.com --project "$PROJECT"
#   gcloud auth configure-docker us-central1-docker.pkg.dev
#
#   # 2) Let the SA sign its own JWT (auth.py keyless signing on Cloud Run)
#   gcloud iam service-accounts add-iam-policy-binding "$SA" \
#       --member="serviceAccount:$SA" \
#       --role="roles/iam.serviceAccountTokenCreator" --project "$PROJECT"
#
#   # (SA already has run.invoker on edw-data-control-center and aiplatform.user.)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# --- read ALL config from .env -----------------------------------------------
ENV_FILE="${ENV_FILE:-./.env}"
[ -f "${ENV_FILE}" ] || { echo "deploy.sh: ${ENV_FILE} not found" >&2; exit 1; }
set -a            # auto-export everything sourced below
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

PROJECT=tecovas-prod-edw
REGION=us-central1
SERVICE="edw-data-control-agent"
AGENT_SA="${SERVICE}@${PROJECT}.iam.gserviceaccount.com"

# Artifact Registry image to build, push, and deploy.
AR_REPO="${AR_REPO:-docker}"   # repo in ${REGION}-docker.pkg.dev/${PROJECT}
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${SERVICE}:${TAG}"

# Required app config (must be present in .env). settings.py reads these names.
: "${CLOUD_RUN_DATA_CONTROL_URL:?set CLOUD_RUN_DATA_CONTROL_URL in .env}"
: "${SLACK_BOT_TOKEN:?set SLACK_BOT_TOKEN in .env}"
: "${SLACK_CHANNEL:?set SLACK_CHANNEL in .env}"
: "${SLACK_SIGNING_SECRET:?set SLACK_SIGNING_SECRET in .env}"

# Forward the runtime env to Cloud Run. We do NOT forward
# GOOGLE_APPLICATION_CREDENTIALS — there is no key file on Cloud Run; auth.py
# signs keylessly as the attached SA instead (discovering its own email from the
# metadata server). Vertex project/location are pinned to the deploy target. The
# "^|^" prefix makes "|" the delimiter so values that contain commas are safe.
ENV_VARS="^|^CLOUD_RUN_DATA_CONTROL_URL=${CLOUD_RUN_DATA_CONTROL_URL}"
ENV_VARS="${ENV_VARS}|SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}"
ENV_VARS="${ENV_VARS}|SLACK_SIGNING_SECRET=${SLACK_SIGNING_SECRET}"
ENV_VARS="${ENV_VARS}|SLACK_CHANNEL=${SLACK_CHANNEL}"
ENV_VARS="${ENV_VARS}|GOOGLE_CLOUD_PROJECT=${PROJECT}"
ENV_VARS="${ENV_VARS}|GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_VARS="${ENV_VARS}|GOOGLE_GENAI_USE_VERTEXAI=${GOOGLE_GENAI_USE_VERTEXAI:-TRUE}"

echo ">> Build image for linux/amd64 (Cloud Run's platform): ${IMAGE}"
# One-time on a new machine: gcloud auth configure-docker ${REGION}-docker.pkg.dev
docker build --platform linux/amd64 -t "${IMAGE}" .

echo ">> Push image to Artifact Registry"
docker push "${IMAGE}"

echo ">> Deploy that image to Cloud Run (IAM-only, no public access)"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-account "${AGENT_SA}" \
  --no-allow-unauthenticated \
  --set-env-vars "${ENV_VARS}"

AGENT_URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --project "${PROJECT}" --format='value(status.url)')"

# Heartbeat: every day, every 25 min during 6:00-8:50 America/Chicago.
SCHEDULE="${SCHEDULE:-*/25 6-8 * * *}"
TIME_ZONE="${TIME_ZONE:-America/Chicago}"
echo ">> Create/refresh the heartbeat (${SCHEDULE} ${TIME_ZONE}) — Scheduler POSTs /run with an OIDC token"
gcloud scheduler jobs create http "${SERVICE}-heartbeat" \
  --project "${PROJECT}" \
  --location "${REGION}" \
  --schedule "${SCHEDULE}" \
  --time-zone "${TIME_ZONE}" \
  --uri "${AGENT_URL}/run" \
  --http-method POST \
  --oidc-service-account-email "${AGENT_SA}" \
  --oidc-token-audience "${AGENT_URL}" \
  2>/dev/null \
  || gcloud scheduler jobs update http "${SERVICE}-heartbeat" \
       --project "${PROJECT}" \
       --location "${REGION}" \
       --schedule "${SCHEDULE}" \
       --time-zone "${TIME_ZONE}" \
       --uri "${AGENT_URL}/run" \
       --http-method POST \
       --oidc-service-account-email "${AGENT_SA}" \
       --oidc-token-audience "${AGENT_URL}"

echo ">> Done. Agent at ${AGENT_URL}"
