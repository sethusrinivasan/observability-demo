# ---------------------------------------------------------------------------
# ECR Container Repository References (Workload Layer)
# ---------------------------------------------------------------------------
# The foundational ECR repositories are provisioned in deployment/terraform-foundation.
# In the workload layer, we reference deterministic ECR image URIs so that
# workload tear-down drills never delete the container images or registries.

locals {
  services = [
    "observability-python-app",
    "observability-java-app",
    "observability-rust-app",
    "observability-node-app",
    "observability-go-app",
    "observability-dotnet-app",
    "observability-c-app",
    "observability-php-app",
    "observability-swift-app",
    "observability-canary",
    "custom-telemetry-receiver"
  ]

  ecr_registry_url = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com"

  service_image_urls = {
    for svc in local.services : svc => "${local.ecr_registry_url}/${svc}"
  }
}
