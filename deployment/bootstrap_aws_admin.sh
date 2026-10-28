#!/usr/bin/env bash
# ==============================================================================
# deployment/bootstrap_aws_admin.sh
#
# Administrative Bootstrap & Self-Healing Script for AWS Observability Demo
#
# Resolves GitHub Actions CI/CD Deployment Pre-requisites:
# 1. Creates foundational backend S3 bucket (observability-demo-telemetry-lake-<ACCOUNT_ID>)
#    with AES256 encryption, public access block, and mandatory ApplicationName tag.
# 2. Registers GitHub Actions OIDC identity provider in AWS IAM.
# 3. Creates/updates IAM role 'observability-demo-github-oidc-role' with trust policy
#    for repo 'sethusrinivasan/observability-demo' and AdministratorAccess.
# 4. Grants fallback CI IAM user ('github-actions-terraform-ci') sts:AssumeRole and S3 rights.
#
# Usage:
#   AWS_REGION=us-east-2 ./deployment/bootstrap_aws_admin.sh
# ==============================================================================

set -euo pipefail

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

# Prerequisites Check
command -v aws >/dev/null 2>&1 || { log_error "aws CLI is required but not installed."; exit 1; }
command -v jq >/dev/null 2>&1 || { log_error "jq is required but not installed."; exit 1; }

# Environment & Variables
AWS_REGION="${AWS_REGION:-us-east-2}"
GITHUB_REPO="${GITHUB_REPO:-sethusrinivasan/observability-demo}"
ROLE_NAME="${ROLE_NAME:-observability-demo-github-oidc-role}"
CI_USER_NAME="${CI_USER_NAME:-github-actions-terraform-ci}"
APP_TAG="Observability-Demo"

log_info "Verifying AWS caller identity..."
CALLER_IDENTITY=$(aws sts get-caller-identity --output json 2>&1) || {
  log_error "Failed to retrieve AWS caller identity. Please configure administrative AWS credentials."
  log_error "${CALLER_IDENTITY}"
  exit 1
}

ACCOUNT_ID=$(echo "${CALLER_IDENTITY}" | jq -r .Account)
CALLER_ARN=$(echo "${CALLER_IDENTITY}" | jq -r .Arn)
STATE_BUCKET="${AWS_S3_BUCKET:-observability-demo-telemetry-lake-${ACCOUNT_ID}}"

echo ""
echo -e "${BOLD}================================================================${NC}"
echo -e "${BOLD}  AWS OBSERVABILITY DEMO — ADMINISTRATIVE BOOTSTRAP${NC}"
echo -e "  Account ID:   ${GREEN}${ACCOUNT_ID}${NC}"
echo -e "  Caller ARN:   ${CYAN}${CALLER_ARN}${NC}"
echo -e "  Region:       ${CYAN}${AWS_REGION}${NC}"
echo -e "  State Bucket: ${GREEN}${STATE_BUCKET}${NC}"
echo -e "  OIDC Role:    ${GREEN}${ROLE_NAME}${NC}"
echo -e "  GitHub Repo:  ${CYAN}${GITHUB_REPO}${NC}"
echo -e "${BOLD}================================================================${NC}"
echo ""

# ------------------------------------------------------------------------------
# 1. Foundational S3 Bucket
# ------------------------------------------------------------------------------
log_info "Step 1: Checking foundational S3 bucket: ${STATE_BUCKET}..."
if aws s3api head-bucket --bucket "${STATE_BUCKET}" 2>/dev/null; then
  log_success "Foundational S3 bucket '${STATE_BUCKET}' already exists and is accessible."
else
  log_info "Bucket '${STATE_BUCKET}' does not exist. Creating in region '${AWS_REGION}'..."
  if [ "${AWS_REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${AWS_REGION}"
  else
    aws s3api create-bucket --bucket "${STATE_BUCKET}" --region "${AWS_REGION}" \
      --create-bucket-configuration LocationConstraint="${AWS_REGION}"
  fi
  log_success "Created S3 bucket: ${STATE_BUCKET}"
fi

log_info "Configuring bucket security & lifecycle settings..."
aws s3api put-public-access-block \
  --bucket "${STATE_BUCKET}" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" 2>/dev/null || true

aws s3api put-bucket-versioning \
  --bucket "${STATE_BUCKET}" \
  --versioning-configuration Status=Disabled 2>/dev/null || true

aws s3api put-bucket-encryption \
  --bucket "${STATE_BUCKET}" \
  --server-side-encryption-configuration '{"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}' 2>/dev/null || true

aws s3api put-bucket-tagging \
  --bucket "${STATE_BUCKET}" \
  --tagging "TagSet=[{Key=ApplicationName,Value=${APP_TAG}},{Key=ManagedBy,Value=Terraform}]" 2>/dev/null || true

log_success "S3 bucket '${STATE_BUCKET}' configured with encryption, security baseline, and tags."

# ------------------------------------------------------------------------------
# 2. GitHub Actions OIDC Identity Provider
# ------------------------------------------------------------------------------
log_info "Step 2: Checking GitHub Actions OIDC Identity Provider in IAM..."
OIDC_PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"

if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${OIDC_PROVIDER_ARN}" 2>/dev/null; then
  log_success "GitHub OIDC provider '${OIDC_PROVIDER_ARN}' already exists."
else
  log_info "Creating GitHub OIDC identity provider..."
  aws iam create-open-id-connect-provider \
    --url "https://token.actions.githubusercontent.com" \
    --client-id-list "sts.amazonaws.com" \
    --thumbprint-list \
      "6938fd4d98bab03faadb97b34396831e3780aea1" \
      "1c58a3a8518e8759bf075b76b750d4f8d264fcd9"
  log_success "Created GitHub OIDC provider: ${OIDC_PROVIDER_ARN}"
fi

# ------------------------------------------------------------------------------
# 3. IAM Deployment Role (observability-demo-github-oidc-role)
# ------------------------------------------------------------------------------
log_info "Step 3: Checking CI/CD IAM deployment role: ${ROLE_NAME}..."
TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubOIDCAuth",
      "Effect": "Allow",
      "Principal": {
        "Federated": "${OIDC_PROVIDER_ARN}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:${GITHUB_REPO}:*"
        }
      }
    },
    {
      "Sid": "AccountRootAndDirectCIUserAccess",
      "Effect": "Allow",
      "Principal": {
        "AWS": [
          "arn:aws:iam::${ACCOUNT_ID}:root",
          "arn:aws:iam::${ACCOUNT_ID}:user/${CI_USER_NAME}"
        ]
      },
      "Action": [
        "sts:AssumeRole",
        "sts:TagSession"
      ]
    }
  ]
}
EOF
)

if aws iam get-role --role-name "${ROLE_NAME}" 2>/dev/null; then
  log_info "Updating trust policy on role '${ROLE_NAME}'..."
  aws iam update-assume-role-policy \
    --role-name "${ROLE_NAME}" \
    --policy-document "${TRUST_POLICY}"
  log_success "Trust policy updated for role '${ROLE_NAME}'."
else
  log_info "Creating IAM role '${ROLE_NAME}'..."
  aws iam create-role \
    --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "${TRUST_POLICY}" \
    --description "Role assumed by GitHub Actions for observability demo deployments" \
    --tags Key=ApplicationName,Value="${APP_TAG}"
  log_success "Created IAM role: ${ROLE_NAME}"
fi

log_info "Ensuring AdministratorAccess policy is attached to '${ROLE_NAME}'..."
aws iam attach-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess" 2>/dev/null || true
log_success "Attached AdministratorAccess to '${ROLE_NAME}'."

# ------------------------------------------------------------------------------
# 4. Fallback CI IAM User Permissions (github-actions-terraform-ci)
# ------------------------------------------------------------------------------
log_info "Step 4: Checking CI IAM user '${CI_USER_NAME}'..."
if aws iam get-user --user-name "${CI_USER_NAME}" 2>/dev/null; then
  log_info "Granting '${CI_USER_NAME}' permissions to assume '${ROLE_NAME}' and manage S3 backend..."
  CI_USER_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAssumeOidcRole",
      "Effect": "Allow",
      "Action": [
        "sts:AssumeRole",
        "sts:TagSession"
      ],
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
    },
    {
      "Sid": "AllowS3BackendBucket",
      "Effect": "Allow",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::${STATE_BUCKET}",
        "arn:aws:s3:::${STATE_BUCKET}/*"
      ]
    }
  ]
}
EOF
  )
  aws iam put-user-policy \
    --user-name "${CI_USER_NAME}" \
    --policy-name "ObservabilityDemoCIPolicy" \
    --policy-document "${CI_USER_POLICY}"
  log_success "Attached inline policy 'ObservabilityDemoCIPolicy' to '${CI_USER_NAME}'."
else
  log_warn "IAM user '${CI_USER_NAME}' does not exist (skipping user policy attachment; OIDC will be used directly)."
fi

# ------------------------------------------------------------------------------
# 5. Summary & Verification
# ------------------------------------------------------------------------------
echo ""
echo -e "${BOLD}================================================================${NC}"
echo -e "${GREEN}${BOLD}  ADMINISTRATIVE BOOTSTRAP COMPLETE!${NC}"
echo -e "${BOLD}================================================================${NC}"
echo -e "  Backend S3 Bucket:  s3://${STATE_BUCKET}"
echo -e "  OIDC Identity:      ${OIDC_PROVIDER_ARN}"
echo -e "  CI/CD Role:         arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo -e "  Trust Subject:      repo:${GITHUB_REPO}:*"
echo -e "${BOLD}================================================================${NC}"
echo ""
log_success "All prerequisites for GitHub Actions AWS deployment are satisfied."
