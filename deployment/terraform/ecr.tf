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
}

resource "aws_ecr_repository" "services" {
  for_each             = toset(local.services)
  name                 = each.value
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Frugality Lifecycle: Only keep last 3 tagged images and purge untagged images after 1 day
resource "aws_ecr_lifecycle_policy" "services" {
  for_each   = aws_ecr_repository.services
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep only the last 3 tagged images to minimize ECR storage cost"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "latest", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 3
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
