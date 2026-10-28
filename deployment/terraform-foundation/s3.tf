locals {
  bucket_name = coalesce(
    var.s3_bucket_name,
    "observability-demo-telemetry-lake-${data.aws_caller_identity.current.account_id}"
  )
}

resource "aws_s3_bucket" "telemetry_lake" {
  bucket        = local.bucket_name
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "telemetry_lake" {
  bucket = aws_s3_bucket.telemetry_lake.id
  versioning_configuration {
    status = "Disabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "telemetry_lake" {
  bucket = aws_s3_bucket.telemetry_lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "telemetry_lake" {
  bucket = aws_s3_bucket.telemetry_lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Frugal Lifecycle Policy: Transitions to Standard-IA after 30 days and Glacier after 90 days
resource "aws_s3_bucket_lifecycle_configuration" "telemetry_lake" {
  bucket = aws_s3_bucket.telemetry_lake.id

  rule {
    id     = "telemetry-parquet-lifecycle"
    status = "Enabled"

    filter {
      prefix = "telemetry/"
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    expiration {
      days = 365
    }
  }
}
