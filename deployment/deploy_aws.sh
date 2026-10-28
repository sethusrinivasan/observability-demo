#!/usr/bin/env bash
# ==============================================================================
# deploy_aws.sh — Frugal AWS Lambda MicroVM & Telemetry Lake Deployment Script
# 
# Manages infrastructure via Terraform with mandatory ApplicationName tag:
#   ApplicationName="Observability-Demo"
#
# Usage:
#   ./deploy_aws.sh init                 Initialize Terraform
#   ./deploy_aws.sh plan                 Show proposed infrastructure changes
#   ./deploy_aws.sh apply                Deploy/update infrastructure
#   ./deploy_aws.sh build-push [SERVICE] Build arm64 image and push to ECR (default: all)
#   ./deploy_aws.sh deploy [SERVICE]     Build, push, and update Lambda function code
#   ./deploy_aws.sh status               Display live Function URLs & resource tags
#   ./deploy_aws.sh destroy              Tear down AWS resources safely
# ==============================================================================

set -euo pipefail

# Auto-elevate group permissions if user is member of docker group in /etc/group
if ! docker ps >/dev/null 2>&1 && command -v sg >/dev/null 2>&1 && sg docker -c "docker ps" >/dev/null 2>&1; then
    exec sg docker -c "\"$0\" $*"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform"
FOUNDATION_DIR="${SCRIPT_DIR}/terraform-foundation"

APP_TAG="Observability-Demo"
AWS_REGION="${AWS_REGION:-us-east-2}"

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

check_prereqs() {
  local missing=()
  command -v terraform >/dev/null 2>&1 || missing+=("terraform (https://www.terraform.io)")
  command -v aws >/dev/null 2>&1 || missing+=("aws-cli (https://aws.amazon.com/cli)")
  command -v docker >/dev/null 2>&1 || missing+=("docker")

  if [ ${#missing[@]} -gt 0 ]; then
    log_error "Missing required prerequisites:"
    for m in "${missing[@]}"; do
      echo -e "  - ${m}"
    done
    exit 1
  fi

  if [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
    eval $(aws configure export-credentials --format env 2>/dev/null || true)
  fi

  # Auto-export credentials if using AWS CLI v2 login session
  eval $(aws configure export-credentials --format env 2>/dev/null || true)
}

get_aws_account_id() {
  aws sts get-caller-identity --query "Account" --output text 2>/dev/null || {
    log_error "Unable to authenticate with AWS. Please configure credentials (e.g. 'aws configure')."
    exit 1
  }
}

get_ecr_login() {
  local account_id
  account_id=$(get_aws_account_id)
  log_info "Logging into Amazon ECR: ${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null 2>&1
  log_success "ECR login successful"
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

build_and_push_service() {
  local svc_name="$1"
  local account_id
  account_id=$(get_aws_account_id)
  local repo_url="${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com/${svc_name}"

  log_info "Locating Dockerfile for ${svc_name}..."
  local dockerfile=""
  for entry in "${SERVICES[@]}"; do
    local key="${entry%%:*}"
    local path="${entry##*:}"
    if [ "${key}" = "${svc_name}" ]; then
      dockerfile="${path}"
      break
    fi
  done

  if [ -z "${dockerfile}" ] || [ ! -f "${REPO_ROOT}/${dockerfile}" ]; then
    log_error "Dockerfile for service '${svc_name}' not found at: ${dockerfile}"
    return 1
  fi

  local context_dir
  context_dir="$(dirname "${REPO_ROOT}/${dockerfile}")"

  local platform="linux/arm64"
  if [ "${svc_name}" = "observability-swift-app" ]; then
    platform="linux/amd64"
  fi

  log_info "Building ${platform} image for ${BOLD}${svc_name}${NC} (Tagged: ApplicationName=${APP_TAG})..."
  docker buildx build \
    --platform "${platform}" \
    --provenance=false \
    --build-arg APPLICATION_NAME="${APP_TAG}" \
    -t "${repo_url}:latest" \
    -f "${REPO_ROOT}/${dockerfile}" \
    "${context_dir}" \
    --push

  log_success "Built & pushed: ${repo_url}:latest"
}

cmd_foundation_init() {
  check_prereqs
  log_info "Initializing Foundational Terraform in ${FOUNDATION_DIR}..."
  terraform -chdir="${FOUNDATION_DIR}" init -backend=false
  log_success "Foundational Terraform initialized."
}

cmd_foundation_plan() {
  check_prereqs
  log_info "Running Plan for Foundational Infrastructure (S3 Lake, CI OIDC Role, ECR)..."
  terraform -chdir="${FOUNDATION_DIR}" plan -var="aws_region=${AWS_REGION}"
}

cmd_foundation_apply() {
  check_prereqs
  log_info "Applying Foundational Infrastructure for ${BOLD}${APP_TAG}${NC} in ${AWS_REGION}..."
  terraform -chdir="${FOUNDATION_DIR}" apply -auto-approve -var="aws_region=${AWS_REGION}"
  log_success "Foundational infrastructure applied and active."
}

cmd_init() {
  check_prereqs
  log_info "Initializing Terraform in ${TF_DIR}..."
  terraform -chdir="${TF_DIR}" init
  log_success "Terraform initialized."
}

cmd_plan() {
  check_prereqs
  log_info "Running Terraform Plan (Enforcing default tag: ApplicationName=${APP_TAG})..."
  terraform -chdir="${TF_DIR}" plan -var="aws_region=${AWS_REGION}"
}

cmd_apply() {
  check_prereqs
  log_info "Applying Terraform Infrastructure for ${BOLD}${APP_TAG}${NC} in ${AWS_REGION}..."
  terraform -chdir="${TF_DIR}" apply -auto-approve -var="aws_region=${AWS_REGION}"
  log_success "Terraform apply completed."
  cmd_status
}

cmd_build_push() {
  check_prereqs
  get_ecr_login
  local target="${1:-all}"

  if [ "${target}" = "all" ]; then
    log_info "Building and pushing all 9 service containers for arm64 Graviton..."
    for entry in "${SERVICES[@]}"; do
      local svc_name="${entry%%:*}"
      build_and_push_service "${svc_name}"
    done
  else
    build_and_push_service "${target}"
  fi
  log_success "Build and push completed."
}

cmd_deploy() {
  check_prereqs
  local target="${1:-all}"
  cmd_build_push "${target}"

  log_info "Updating Lambda function code in AWS..."
  local account_id
  account_id=$(get_aws_account_id)

  local lambda_map=(
    "observability-python-app:observability-python-app"
    "observability-java-app:observability-java-app"
    "observability-rust-app:observability-rust-app"
    "observability-node-app:observability-node-app"
    "observability-go-app:observability-go-app"
    "observability-dotnet-app:observability-dotnet-app"
    "observability-c-app:observability-c-app"
    "observability-php-app:observability-php-app"
    "observability-swift-app:observability-swift-app"
    "observability-canary:observability-canary-dashboard"
    "custom-telemetry-receiver:observability-telemetry-collector"
  )

  for entry in "${lambda_map[@]}"; do
    local svc="${entry%%:*}"
    local fn="${entry##*:}"
    if [ "${target}" = "all" ] || [ "${target}" = "${svc}" ]; then
      log_info "Updating Lambda function: ${fn}..."
      aws lambda update-function-code \
        --region "${AWS_REGION}" \
        --function-name "${fn}" \
        --image-uri "${account_id}.dkr.ecr.${AWS_REGION}.amazonaws.com/${svc}:latest" >/dev/null
      log_success "Updated ${fn}"
    fi
  done
  cmd_status
}

cmd_status() {
  check_prereqs
  if [ ! -d "${TF_DIR}/.terraform" ]; then
    log_warn "Terraform is not initialized. Run './deploy_aws.sh init' first."
    return
  fi

  log_info "Fetching live AWS deployment outputs from Terraform..."
  echo ""
  echo -e "${BOLD}================================================================${NC}"
  echo -e "${BOLD}  OBSERVABILITY DEMO — LIVE AWS DEPLOYMENT STATUS${NC}"
  echo -e "  ApplicationName Tag: ${GREEN}${APP_TAG}${NC}"
  echo -e "  AWS Region:          ${CYAN}${AWS_REGION}${NC}"
  echo -e "${BOLD}================================================================${NC}"
  
  terraform -chdir="${TF_DIR}" output || true
  echo ""
}

cmd_destroy() {
  check_prereqs
  echo -e "${RED}${BOLD}WARNING: This will destroy all AWS resources created for ${APP_TAG}!${NC}"
  read -r -p "Type 'DESTROY' to confirm: " confirm
  if [ "${confirm}" = "DESTROY" ]; then
    terraform -chdir="${TF_DIR}" destroy -auto-approve -var="aws_region=${AWS_REGION}"
    log_success "All resources destroyed."
  else
    log_info "Destroy cancelled."
  fi
}

archive_active_s3_bucket() {
  check_prereqs
  local account_id
  account_id=$(get_aws_account_id)
  local base_bucket="${S3_BUCKET_NAME:-observability-demo-telemetry-lake-${account_id}}"

  log_info "Checking if active S3 bucket '${base_bucket}' exists..."
  if ! aws s3api head-bucket --bucket "${base_bucket}" 2>/dev/null; then
    log_info "Active bucket '${base_bucket}' does not exist. Skipping archive."
    return 0
  fi

  local obj_count
  obj_count=$(aws s3api list-objects-v2 --bucket "${base_bucket}" --max-items 1 --query "KeyCount" --output text 2>/dev/null || echo "0")
  if [ "${obj_count}" = "0" ] || [ "${obj_count}" = "None" ]; then
    log_info "Bucket '${base_bucket}' is empty. No objects to archive."
    return 0
  fi

  local suffix=1
  local archive_bucket="${base_bucket}-${suffix}"
  while aws s3api head-bucket --bucket "${archive_bucket}" 2>/dev/null; do
    suffix=$((suffix + 1))
    archive_bucket="${base_bucket}-${suffix}"
  done

  log_info "Renaming active bucket with numbered suffix: archiving '${base_bucket}' -> '${archive_bucket}'..."
  if [ "${AWS_REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${archive_bucket}" --region "${AWS_REGION}"
  else
    aws s3api create-bucket --bucket "${archive_bucket}" --region "${AWS_REGION}" \
      --create-bucket-configuration LocationConstraint="${AWS_REGION}"
  fi

  aws s3api put-bucket-tagging --bucket "${archive_bucket}" --tagging \
    "TagSet=[{Key=ApplicationName,Value=${APP_TAG}},{Key=ArchivedFrom,Value=${base_bucket}},{Key=ArchiveIndex,Value=${suffix}}]" 2>/dev/null || true

  log_info "Syncing objects from '${base_bucket}' to '${archive_bucket}'..."
  aws s3 sync "s3://${base_bucket}" "s3://${archive_bucket}"

  log_info "Clearing non-state objects from active bucket '${base_bucket}' prior to destroy..."
  aws s3 rm "s3://${base_bucket}" --recursive --exclude "terraform/state/*" 2>/dev/null || true
  log_success "Active bucket archived as '${archive_bucket}' with all objects preserved."
}

cmd_redeploy() {
  check_prereqs
  echo -e "${RED}${BOLD}WARNING: This will archive S3 data with a numbered suffix, tear down workload resources for ${APP_TAG}, and redeploy fully!${NC}"
  echo -e "${CYAN}Note: Foundational resources (S3 state bucket, CI OIDC role, ECR registries) are isolated in terraform-foundation and will not be touched.${NC}"
  read -r -p "Type 'DESTROY' to confirm: " confirm
  if [ "${confirm}" = "DESTROY" ]; then
    archive_active_s3_bucket
    log_info "Destroying application workload resources in ${TF_DIR}..."
    terraform -chdir="${TF_DIR}" destroy -auto-approve -var="aws_region=${AWS_REGION}"
    log_success "Workload resources torn down cleanly. Redeploying infrastructure..."
    terraform -chdir="${TF_DIR}" plan -var="aws_region=${AWS_REGION}"
    terraform -chdir="${TF_DIR}" apply -auto-approve -var="aws_region=${AWS_REGION}"
    log_success "Redeployment completed."
    cmd_status
  else
    log_info "Redeploy cancelled."
  fi
}

cmd_help() {
  cat <<EOF
Usage: ./deploy_aws.sh [COMMAND] [ARGS]

Commands:
  foundation-init          Initialize foundational Terraform (S3 Lake, OIDC Role, ECR)
  foundation-plan          Show proposed changes to foundational infrastructure
  foundation-apply         Provision or update foundational infrastructure
  init                     Initialize workload Terraform providers and modules
  plan                     Show proposed changes to workload AWS infrastructure
  apply                    Provision or update workload infrastructure via Terraform
  build-push [SERVICE]     Build arm64 Graviton container and push to ECR (default: all)
  deploy [SERVICE]         Build, push to ECR, and update Lambda function code
  status                   Display live HTTPS Function URLs & outputs
  destroy                  Tear down AWS workload resources safely
  destroy-and-redeploy     Destroy and redeploy AWS workload infrastructure fully
  help                     Show this help message

Options / Environment Variables:
  AWS_REGION               AWS Region to target (default: us-east-2)
  ApplicationName Tag      Mandatory project tag: "Observability-Demo" (applied automatically)

Available Services:
  - observability-python-app
  - observability-java-app
  - observability-rust-app
  - observability-node-app
  - observability-go-app
  - observability-dotnet-app
  - observability-c-app
  - observability-php-app
  - observability-swift-app
  - observability-canary
  - custom-telemetry-receiver
  - all (default)
EOF
}

# Main Dispatcher
case "${1:-help}" in
  foundation-init)  cmd_foundation_init ;;
  foundation-plan)  cmd_foundation_plan ;;
  foundation-apply) cmd_foundation_apply ;;
  init)       cmd_init ;;
  plan)       cmd_plan ;;
  apply)      cmd_apply ;;
  build-push) shift; cmd_build_push "${1:-all}" ;;
  deploy)     shift; cmd_deploy "${1:-all}" ;;
  status)     cmd_status ;;
  destroy)    cmd_destroy ;;
  redeploy|destroy-and-redeploy) cmd_redeploy ;;
  help|--help|-h) cmd_help ;;
  *)
    log_error "Unknown command: $1"
    cmd_help
    exit 1
    ;;
esac
