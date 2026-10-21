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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/terraform"

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
  "observability-python-app:python-app/Dockerfile"
  "observability-java-app:java-app/Dockerfile"
  "observability-rust-app:rust-app/Dockerfile"
  "observability-node-app:node-app/Dockerfile"
  "observability-go-app:go-app/Dockerfile"
  "observability-dotnet-app:dotnet-app/Dockerfile"
  "observability-c-app:c-app/Dockerfile"
  "observability-canary:canary/Dockerfile"
  "custom-telemetry-receiver:telemetry-receiver/Dockerfile"
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

  if [ -z "${dockerfile}" ] || [ ! -f "${SCRIPT_DIR}/${dockerfile}" ]; then
    log_error "Dockerfile for service '${svc_name}' not found at: ${dockerfile}"
    return 1
  fi

  local context_dir
  context_dir="$(dirname "${SCRIPT_DIR}/${dockerfile}")"
  # For apps that share common scripts or root context
  if [ "${svc_name}" = "observability-canary" ] || [ "${svc_name}" = "custom-telemetry-receiver" ]; then
    context_dir="${SCRIPT_DIR}"
  fi

  log_info "Building arm64 Graviton image for ${BOLD}${svc_name}${NC} (Tagged: ApplicationName=${APP_TAG})..."
  docker buildx build \
    --platform linux/arm64 \
    --provenance=false \
    --build-arg APPLICATION_NAME="${APP_TAG}" \
    -t "${repo_url}:latest" \
    -f "${SCRIPT_DIR}/${dockerfile}" \
    "${context_dir}" \
    --push

  log_success "Built & pushed: ${repo_url}:latest"
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

cmd_help() {
  cat <<EOF
Usage: ./deploy_aws.sh [COMMAND] [ARGS]

Commands:
  init                     Initialize Terraform providers and modules
  plan                     Show proposed changes to AWS infrastructure
  apply                    Provision or update infrastructure via Terraform
  build-push [SERVICE]     Build arm64 Graviton container and push to ECR (default: all)
  deploy [SERVICE]         Build, push to ECR, and update Lambda function code
  status                   Display live HTTPS Function URLs & outputs
  destroy                  Tear down AWS resources safely
  help                     Show this help message

Options / Environment Variables:
  AWS_REGION               AWS Region to target (default: us-east-1)
  ApplicationName Tag      Mandatory project tag: "Observability-Demo" (applied automatically)

Available Services:
  - observability-python-app
  - observability-java-app
  - observability-rust-app
  - observability-node-app
  - observability-go-app
  - observability-dotnet-app
  - observability-c-app
  - observability-canary
  - custom-telemetry-receiver
  - all (default)
EOF
}

# Main Dispatcher
case "${1:-help}" in
  init)       cmd_init ;;
  plan)       cmd_plan ;;
  apply)      cmd_apply ;;
  build-push) shift; cmd_build_push "${1:-all}" ;;
  deploy)     shift; cmd_deploy "${1:-all}" ;;
  status)     cmd_status ;;
  destroy)    cmd_destroy ;;
  help|--help|-h) cmd_help ;;
  *)
    log_error "Unknown command: $1"
    cmd_help
    exit 1
    ;;
esac
