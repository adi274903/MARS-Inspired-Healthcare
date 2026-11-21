#!/usr/bin/env bash
# Deploy MARS-Healthcare to Cloud Run with Terraform.
#   PROJECT_ID=my-project ./infra/deploy.sh
# Needs: gcloud (logged in: `gcloud auth login` + `gcloud auth application-default login`), terraform,
# the local data/papers.db + data/qdrant, and models/embeddinggemma-2 (copied from the HF cache).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF="$ROOT/infra/terraform"
export PATH="$HOME/google-cloud-sdk/bin:$PATH"

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
BILLING_ACCOUNT_ID="${BILLING_ACCOUNT_ID:-$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)' 2>/dev/null | sed 's#billingAccounts/##')}"
TAG="${TAG:-$(git -C "$ROOT" rev-parse --short HEAD)-$(date +%Y%m%d%H%M)}"

for f in data/papers.db data/qdrant models/embeddinggemma-2/model.safetensors; do
  [[ -e "$ROOT/$f" ]] || { echo "missing $f" >&2; exit 1; }
done

export TF_VAR_project_id="$PROJECT_ID" TF_VAR_region="$REGION" TF_VAR_image_tag="$TAG" TF_VAR_billing_account_id="$BILLING_ACCOUNT_ID"
cd "$TF"
# Terraform uses Application Default Credentials if present, else a fresh gcloud access token per step (1 h lifetime).
tf() {
  if [[ ! -f "$HOME/.config/gcloud/application_default_credentials.json" ]]; then
    export GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token)"
  fi
  terraform "$@"
}
# Newly enabled APIs take minutes to propagate; retry each apply instead of failing on the first 403.
tf_apply() {
  for i in 1 2 3 4 5 6; do
    tf apply -input=false -auto-approve "$@" && return 0
    echo "apply failed (attempt $i/6), retrying in 30 s" >&2; sleep 30
  done
  return 1
}
tf init -input=false -upgrade >/dev/null

echo "== 1/3 APIs, registry, secrets, IAM"
# Terraform itself needs these two before it can manage any other API (a new project has them off).
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com --project="$PROJECT_ID"
tf_apply -target=google_project_service.apis
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
for _ in $(seq 1 30); do  # the Compute default SA appears shortly after compute.googleapis.com is enabled
  gcloud iam service-accounts describe "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --project="$PROJECT_ID" >/dev/null 2>&1 && break
  sleep 10
done
tf_apply \
  -target=google_artifact_registry_repository.mars \
  -target=google_project_iam_member.build_sa \
  -target=google_secret_manager_secret.openrouter \
  -target=google_secret_manager_secret.admin \
  -target=google_service_account.run \
  -target=google_secret_manager_secret_iam_member.run_access

echo "== 2/3 secret values (read locally, never printed, never in tfstate)"
if ! gcloud secrets versions list openrouter-api-key --project="$PROJECT_ID" --format='value(name)' --limit=1 | grep -q .; then
  grep -E '^OPENROUTER_API_KEY=' "$ROOT/.env" | head -1 | cut -d= -f2- | tr -d '\n' \
    | gcloud secrets versions add openrouter-api-key --project="$PROJECT_ID" --data-file=- >/dev/null
fi
if ! gcloud secrets versions list mars-admin-token --project="$PROJECT_ID" --format='value(name)' --limit=1 | grep -q .; then
  openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add mars-admin-token --project="$PROJECT_ID" --data-file=- >/dev/null
fi

echo "== base image (deps + embedding model + data; built once, code-only layers go on top)"
REPO="$REGION-docker.pkg.dev/$PROJECT_ID/mars/mars"
if ! gcloud artifacts docker tags list "$REPO" --project="$PROJECT_ID" --format='value(tag)' | grep -q 'base$'; then
  if [[ -n "${BASE_SRC_TAG:-}" ]]; then  # reuse an image that already holds everything
    gcloud artifacts docker tags add "$REPO:$BASE_SRC_TAG" "$REPO:base" --project="$PROJECT_ID"
  else                                    # fresh project: full build from ./Dockerfile (uploads data/ and models/)
    gcloud builds submit "$ROOT" --project="$PROJECT_ID" --tag "$REPO:base" --timeout=3600s
  fi
fi

echo "== 3/3 build code layer ($TAG) in Cloud Build, then deploy"
tf apply -input=false -auto-approve -refresh=false -target=terraform_data.image  # single attempt: a failed build should stop, not re-upload 1.9 GB six times
tf_apply
tf output -raw service_url; echo
