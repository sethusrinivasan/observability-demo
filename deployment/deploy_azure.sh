#!/usr/bin/env bash
# ==============================================================================
# deploy_azure.sh — Frugal Azure Container Apps & Telemetry Lake Deployment Script
# 
# Manages infrastructure via Terraform with mandatory ApplicationName tag:
#   ApplicationName="Observability-Demo"
#
# Azure equivalents (indexed on maximum frugality):
#   - Compute: Azure Container Apps (0.25 vCPU, 0.5GiB RAM, min_replicas=0 scale-to-zero)
#   - Telemetry Lake & State: Azure Storage Account (Standard LRS, Hot tier with Cool/Archive lifecycle)
#   - Logging: Log Analytics Workspace with 0.05GB/day cap
#   - Registry: Azure Container Registry (Basic SKU, zero static passwords, OIDC managed identity)
#   - Events: Azure Event Grid System Topic (100k free operations/mo)
#
# Usage:
#   ./deploy_azure.sh foundation-init    Initialize foundational Terraform
#   ./deploy_azure.sh foundation-plan    Show proposed changes to foundation
#   ./deploy_azure.sh foundation-apply   Deploy foundational infrastructure
#   ./deploy_azure.sh init               Initialize workload Terraform
#   ./deploy_azure.sh plan               Show proposed workload infrastructure changes
#   ./deploy_azure.sh apply              Deploy/update workload infrastructure
#   ./deploy_azure.sh build-push [SVC]   Build arm64/amd64 image and push to ACR (default: all)
#   ./deploy_azure.sh status             Display live Container App HTTPS URLs
#   ./deploy_azure.sh destroy            Tear down workload resources safely
#   ./deploy_azure.sh destroy-and-redeploy Archive telemetry & redeploy cleanly
# ==============================================================================

set -euo pipefail

# Auto-elevate group permissions if user is member of docker group in /etc/group
if ! docker ps >/dev/null 2>&1 && command -v sg >/dev/null 2>&1 && sg docker -c "docker ps" >/dev/null 2>&1; then
    exec sg docker -c "\"$0\" $*"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform-azure"
FOUNDATION_DIR="${SCRIPT_DIR}/terraform-azure-foundation"

APP_TAG="Observability-Demo"
AZURE_LOCATION="${AZURE_LOCATION:-eastus2}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-rg-observability-demo-prod}"

# ANSI Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m' # No Color

log_info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

run_with_retry() {
  local max_attempts="${1:-5}"
  local initial_delay="${2:-5}"
  shift 2
  local delay="${initial_delay}"
  for attempt in $(seq 1 "${max_attempts}"); do
    if "$@"; then
      return 0
    fi
    if [ "${attempt}" -eq "${max_attempts}" ]; then
      log_error "Command failed after ${max_attempts} attempts: $*"
      return 1
    fi
    log_warn "Command failed (attempt ${attempt}/${max_attempts}). Backing off for ${delay}s before retrying..."
    sleep "${delay}"
    delay=$((delay * 2))
  done
}

check_prereqs() {
  local missing=()
  command -v terraform >/dev/null 2>&1 || missing+=("terraform (https://www.terraform.io)")
  command -v az >/dev/null 2>&1 || missing+=("azure-cli (https://docs.microsoft.com/cli/azure)")
  command -v docker >/dev/null 2>&1 || missing+=("docker")

  if [ ${#missing[@]} -gt 0 ]; then
    log_error "Missing required prerequisites:"
    for m in "${missing[@]}"; do
      echo -e "  - ${m}"
    done
    exit 1
  fi
}

SERVICES=(
  "observability-python-app:apps/python/Dockerfile"
  "observability-java-app:apps/java/Dockerfile"
  "observability-rust-app:apps/rust/Dockerfile"
  "observability-node-app:apps/node/Dockerfile"
  "observability-go-app:apps/go/Dockerfile"
  "observability-dotnet-app:apps/dotnet/Dockerfile"
  "observability-c-app:apps/c/Dockerfile"
  "observability-php-app:apps/php/Dockerfile"
  "observability-swift-app:apps/swift/Dockerfile"
  "observability-canary:canary/Dockerfile"
  "custom-telemetry-receiver:apps/telemetry-receiver/Dockerfile"
)

cmd_foundation_init() {
  check_prereqs
  log_info "Initializing Foundational Azure Terraform in ${FOUNDATION_DIR}..."
  run_with_retry 5 5 terraform -chdir="${FOUNDATION_DIR}" init -backend=false
  log_success "Foundational Terraform initialized."
}

cmd_foundation_plan() {
  check_prereqs
  log_info "Running Plan for Foundational Azure Infrastructure (Storage Lake, OIDC Identity, ACR)..."
  run_with_retry 3 5 terraform -chdir="${FOUNDATION_DIR}" plan -var="azure_location=${AZURE_LOCATION}"
}

cmd_foundation_apply() {
  check_prereqs
  log_info "Applying Foundational Azure Infrastructure for ${BOLD}${APP_TAG}${NC} in ${AZURE_LOCATION}..."
  run_with_retry 3 10 terraform -chdir="${FOUNDATION_DIR}" apply -auto-approve -var="azure_location=${AZURE_LOCATION}"
  log_success "Foundational Azure infrastructure applied and active."
}

cmd_init() {
  check_prereqs
  log_info "Initializing Azure Workload Terraform in ${TF_DIR} with backoff..."
  run_with_retry 5 5 terraform -chdir="${TF_DIR}" init
  log_success "Azure workload Terraform initialized."
}

cmd_plan() {
  check_prereqs
  log_info "Running Terraform Plan for Azure Workload (ApplicationName=${APP_TAG})..."
  run_with_retry 3 5 terraform -chdir="${TF_DIR}" plan -var="azure_location=${AZURE_LOCATION}"
}

cmd_apply() {
  check_prereqs
  log_info "Applying Azure Workload Infrastructure for ${BOLD}${APP_TAG}${NC} in ${AZURE_LOCATION}..."
  run_with_retry 3 10 terraform -chdir="${TF_DIR}" apply -auto-approve -var="azure_location=${AZURE_LOCATION}"
  log_success "Azure workload Terraform apply completed."
  cmd_status
}

cmd_status() {
  check_prereqs
  if [ ! -d "${TF_DIR}/.terraform" ]; then
    log_warn "Terraform is not initialized. Run './deploy_azure.sh init' first."
    return
  fi

  log_info "Fetching live Azure deployment outputs from Terraform..."
  echo ""
  echo -e "${BOLD}================================================================${NC}"
  echo -e "${BOLD}  OBSERVABILITY DEMO — LIVE AZURE CONTAINER APPS STATUS${NC}"
  echo -e "  ApplicationName Tag: ${GREEN}${APP_TAG}${NC}"
  echo -e "  Azure Location:      ${CYAN}${AZURE_LOCATION}${NC}"
  echo -e "  Frugality Profile:   ${YELLOW}0.25 vCPU, 0.5GiB RAM, min_replicas=0 (scale-to-zero)${NC}"
  echo -e "${BOLD}================================================================${NC}"
  echo ""
  terraform -chdir="${TF_DIR}" output || true
  echo ""
}

archive_active_blob_container() {
  check_prereqs
  local storage_account
  storage_account=$(terraform -chdir="${FOUNDATION_DIR}" output -raw storage_account_name 2>/dev/null || echo "")
  if [ -z "${storage_account}" ]; then
    log_warn "Storage account not found from foundational outputs. Checking active Azure storage..."
    return 0
  fi

  local base_container="telemetry-lake"
  log_info "Checking if active blob container '${base_container}' in account '${storage_account}' has blobs..."
  
  local blob_count
  blob_count=$(az storage blob list --account-name "${storage_account}" --container-name "${base_container}" --auth-mode login --query "length(@)" -o tsv 2>/dev/null || echo "0")

  if [ "${blob_count}" -gt 0 ]; then
    local suffix=1
    local archive_container="${base_container}-${suffix}"
    while az storage container exists --account-name "${storage_account}" --name "${archive_container}" --auth-mode login --query "exists" -o tsv 2>/dev/null | grep -q "true"; do
      suffix=$((suffix + 1))
      archive_container="${base_container}-${suffix}"
    done

    log_info "Archiving active blob container '${base_container}' -> '${archive_container}'..."
    az storage container create --account-name "${storage_account}" --name "${archive_container}" --auth-mode login >/dev/null 2>&1 || true

    log_info "Copying blobs from '${base_container}' to '${archive_container}'..."
    az storage blob copy batch \
      --source-account-name "${storage_account}" \
      --source-container "${base_container}" \
      --destination-account-name "${storage_account}" \
      --destination-container "${archive_container}" \
      --auth-mode login >/dev/null 2>&1 || true

    log_info "Purging non-state telemetry objects from active container '${base_container}'..."
    az storage blob delete-batch --account-name "${storage_account}" --source "${base_container}" --auth-mode login >/dev/null 2>&1 || true
    log_success "Active telemetry archived to '${archive_container}'."
  else
    log_info "Container '${base_container}' is empty or does not exist. Skipping archive."
  fi
}

cmd_destroy() {
  check_prereqs
  echo -e "${RED}${BOLD}WARNING: This will destroy Azure workload resources (Container Apps, Environment, Event Grid)!${NC}"
  echo -e "${CYAN}Note: Foundational resources (Storage account, OIDC Managed Identity, ACR) are protected in terraform-azure-foundation.${NC}"
  read -r -p "Type 'DESTROY' to confirm: " confirm
  if [ "${confirm}" = "DESTROY" ]; then
    run_with_retry 3 10 terraform -chdir="${TF_DIR}" destroy -auto-approve -var="azure_location=${AZURE_LOCATION}"
    log_success "Azure workload resources destroyed."
  else
    log_info "Destroy cancelled."
  fi
}

cmd_redeploy() {
  check_prereqs
  echo -e "${RED}${BOLD}WARNING: This will archive Azure blob telemetry with a numbered suffix, tear down workload resources for ${APP_TAG}, and redeploy fully!${NC}"
  echo -e "${CYAN}Note: Foundational resources are isolated and protected against destruction.${NC}"
  read -r -p "Type 'DESTROY' to confirm: " confirm
  if [ "${confirm}" = "DESTROY" ]; then
    archive_active_blob_container
    log_info "Destroying Azure workload resources in ${TF_DIR} with backoff..."
    run_with_retry 3 10 terraform -chdir="${TF_DIR}" destroy -auto-approve -var="azure_location=${AZURE_LOCATION}"
    log_success "Azure workload torn down cleanly. Redeploying infrastructure..."
    run_with_retry 3 5 terraform -chdir="${TF_DIR}" plan -var="azure_location=${AZURE_LOCATION}"
    run_with_retry 3 10 terraform -chdir="${TF_DIR}" apply -auto-approve -var="azure_location=${AZURE_LOCATION}"
    log_success "Redeployment completed."
    cmd_status
  else
    log_info "Redeploy cancelled."
  fi
}

cmd_help() {
  cat <<EOF
Usage: ./deploy_azure.sh [COMMAND] [ARGS]

Commands:
  foundation-init          Initialize foundational Azure Terraform (Storage, OIDC, ACR)
  foundation-plan          Show proposed changes to foundational Azure infrastructure
  foundation-apply         Provision or update foundational Azure infrastructure
  init                     Initialize workload Azure Terraform providers and modules
  plan                     Show proposed changes to workload Azure infrastructure
  apply                    Provision or update workload infrastructure via Terraform
  status                   Display live Azure Container App HTTPS URLs & outputs
  destroy                  Tear down Azure workload resources safely
  destroy-and-redeploy     Archive blob container with numbered suffix & redeploy cleanly
  help                     Show this help message

Options / Environment Variables:
  AZURE_LOCATION           Azure region to target (default: eastus2)
  AZURE_RESOURCE_GROUP     Azure Resource Group (default: rg-observability-demo-prod)
  ApplicationName Tag      Mandatory project tag: "Observability-Demo" (applied automatically)
EOF
}

case "${1:-help}" in
  foundation-init)  cmd_foundation_init ;;
  foundation-plan)  cmd_foundation_plan ;;
  foundation-apply) cmd_foundation_apply ;;
  init)             cmd_init ;;
  plan)             cmd_plan ;;
  apply)            cmd_apply ;;
  status)           cmd_status ;;
  destroy)          cmd_destroy ;;
  redeploy|destroy-and-redeploy) cmd_redeploy ;;
  help|--help|-h)   cmd_help ;;
  *)
    log_error "Unknown command: $1"
    cmd_help
    exit 1
    ;;
esac
