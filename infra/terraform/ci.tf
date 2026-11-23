# GitHub Actions pushes images through OIDC: short-lived credentials for one workflow
# environment of one repository, and only to this project's registry. No access keys.

locals {
  github_ci = var.github_repository != null
}

resource "aws_iam_openid_connect_provider" "github" {
  count = local.github_ci && var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = local.github_ci && !var.create_github_oidc_provider ? 1 : 0

  url = "https://token.actions.githubusercontent.com"
}

locals {
  github_oidc_provider_arn = !local.github_ci ? null : (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  )
}

resource "aws_iam_role" "ci_image_push" {
  count = local.github_ci ? 1 : 0

  name                 = "${local.name}-ci-image-push"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Only jobs bound to the protected "staging" environment of this repository.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.environment}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ci_image_push" {
  count = local.github_ci ? 1 : 0

  name = "push-images"
  role = aws_iam_role.ci_image_push[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RegistryAuthentication"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*" # this action has no resource
      },
      {
        Sid    = "PushToThisRepository"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Sid      = "EncryptLayers"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = aws_kms_key.main.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "ecr.${var.region}.amazonaws.com" }
        }
      },
    ]
  })
}
