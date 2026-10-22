# ---------------------------------------------------------------------------
# GitHub Actions OIDC Authentication (No Static AWS Keys or Secrets Required)
# ---------------------------------------------------------------------------
# Establishes an IAM OpenID Connect (OIDC) identity provider and assumed role
# allowing GitHub Actions workflow runners in repository 'sethusrinivasan/observability-demo'
# to retrieve short-lived STS session credentials securely.

# 1. GitHub OIDC Identity Provider in AWS IAM
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f8d264fcd9"
  ]

  tags = {
    Name            = "github-actions-oidc-provider"
    ApplicationName = var.app_name
  }
}

# 2. IAM Role Assumed by GitHub Actions Runners
resource "aws_iam_role" "github_actions_terraform" {
  name        = "observability-demo-github-oidc-role"
  description = "Role assumed via OIDC by GitHub Actions to deploy and update Terraform infrastructure"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:sethusrinivasan/observability-demo:*"
          }
        }
      }
    ]
  })

  tags = {
    Name            = "observability-demo-github-oidc-role"
    ApplicationName = var.app_name
  }
}

# 3. Policy Permissions for Terraform Provisioning
resource "aws_iam_policy" "github_actions_terraform_policy" {
  name        = "observability-demo-github-terraform-policy"
  description = "Permissions required for GitHub Actions to manage observability-demo AWS infrastructure"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TerraformResourceManagement"
        Effect = "Allow"
        Action = [
          "lambda:*",
          "s3:*",
          "ecr:*",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:PassRole",
          "iam:ListAttachedRolePolicies",
          "iam:ListRolePolicies",
          "logs:*",
          "events:*",
          "rds:Describe*",
          "rds:ListTagsForResource",
          "cloudwatch:*"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_actions_attach" {
  role       = aws_iam_role.github_actions_terraform.name
  policy_arn = aws_iam_policy.github_actions_terraform_policy.arn
}

output "github_oidc_role_arn" {
  description = "ARN of the IAM role to configure in GitHub Actions (role-to-assume)"
  value       = aws_iam_role.github_actions_terraform.arn
}
