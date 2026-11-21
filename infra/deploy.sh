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
terraform init -input=false -upgrade >/dev/null

echo "== 1/3 APIs, registry, secrets, IAM"
terraform apply -input=false -auto-approve \
  -target=google_project_service.apis \
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

echo "== 3/3 build image ($TAG) and deploy"
terraform apply -input=false -auto-approve
terraform output -raw service_url; echo
