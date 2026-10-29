# Common Assume Role Policy for Lambda
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

# 1. Lambda Execution Role for Polyglot Microservices
resource "aws_iam_role" "microservice_role" {
  name               = "observability-demo-microservice-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "microservice_basic" {
  role       = aws_iam_role.microservice_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# 2. Lambda Execution Role for Custom Telemetry Receiver
resource "aws_iam_role" "collector_role" {
  name               = "observability-demo-collector-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "collector_basic" {
  role       = aws_iam_role.collector_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "collector_s3" {
  name        = "observability-demo-collector-s3-policy"
  description = "Allows custom telemetry collector to write and rotate Parquet files in S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          local.telemetry_lake_arn,
          "${local.telemetry_lake_arn}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "collector_s3_attach" {
  role       = aws_iam_role.collector_role.name
  policy_arn = aws_iam_policy.collector_s3.arn
}

# 3. Lambda Execution Role for Canary Dashboard (with DuckDB S3 Read Access)
resource "aws_iam_role" "canary_role" {
  name               = "observability-demo-canary-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "canary_basic" {
  role       = aws_iam_role.canary_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_policy" "canary_s3_duckdb" {
  name        = "observability-demo-canary-duckdb-s3-policy"
  description = "Allows Canary DuckDB engine and S3 Browser to scan and manage telemetry Parquet files in S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:ListAllMyBuckets"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          local.telemetry_lake_arn,
          "${local.telemetry_lake_arn}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "canary_s3_attach" {
  role       = aws_iam_role.canary_role.name
  policy_arn = aws_iam_policy.canary_s3_duckdb.arn
}

# 4. IAM DB Authentication Policy for Aurora PostgreSQL
resource "aws_iam_policy" "rds_iam_auth" {
  name        = "observability-demo-rds-iam-auth-policy"
  description = "Allows Lambda microVMs and Canary to connect to Aurora PostgreSQL using IAM DB authentication tokens"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "rds-db:connect"
        ]
        Resource = [
          "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.aurora_db_user}",
          "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/observability_reader",
          "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/observability_admin"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "canary_rds_attach" {
  role       = aws_iam_role.canary_role.name
  policy_arn = aws_iam_policy.rds_iam_auth.arn
}

resource "aws_iam_role_policy_attachment" "microservice_rds_attach" {
  role       = aws_iam_role.microservice_role.name
  policy_arn = aws_iam_policy.rds_iam_auth.arn
}

# 5. AWS Service Quotas & Resource Metrics Policy for Canary Dashboard
resource "aws_iam_policy" "canary_service_quotas" {
  name        = "observability-demo-canary-service-quotas-policy"
  description = "Allows Canary Lambda to query AWS Service Quotas and resource usage metrics"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "servicequotas:GetServiceQuota",
          "servicequotas:ListServiceQuotas",
          "servicequotas:ListDefaultServiceQuotas",
          "rds:DescribeDBInstances",
          "rds:DescribeDBClusters",
          "rds:DescribeDBSnapshots",
          "ec2:DescribeInstances",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeAddresses",
          "ec2:DescribeKeyPairs",
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:DescribeInternetGateways",
          "ec2:DescribeNatGateways",
          "lambda:GetAccountSettings",
          "lambda:ListFunctions",
          "cloudwatch:DescribeAlarms",
          "dynamodb:ListTables",
          "dynamodb:DescribeTable"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "canary_service_quotas_attach" {
  role       = aws_iam_role.canary_role.name
  policy_arn = aws_iam_policy.canary_service_quotas.arn
}
