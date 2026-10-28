# ---------------------------------------------------------------------------
# S3 Telemetry Lake Reference (Workload Layer)
# ---------------------------------------------------------------------------
# The foundational S3 bucket is provisioned and lifecycle-managed in
# deployment/terraform-foundation. It is referenced here via static
# configuration / data lookup so weekly workload destroy operations
# never risk destroying the backend state or telemetry data lake.

locals {
  bucket_name = coalesce(
    var.s3_bucket_name,
    "observability-demo-telemetry-lake-${data.aws_caller_identity.current.account_id}"
  )
  telemetry_lake_arn = "arn:aws:s3:::${local.bucket_name}"
}
