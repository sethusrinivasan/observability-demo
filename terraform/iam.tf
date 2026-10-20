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
          aws_s3_bucket.telemetry_lake.arn,
          "${aws_s3_bucket.telemetry_lake.arn}/*"
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
  description = "Allows Canary DuckDB engine to scan telemetry Parquet files in S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = [
          aws_s3_bucket.telemetry_lake.arn,
          "${aws_s3_bucket.telemetry_lake.arn}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "canary_s3_attach" {
  role       = aws_iam_role.canary_role.name
  policy_arn = aws_iam_policy.canary_s3_duckdb.arn
}
