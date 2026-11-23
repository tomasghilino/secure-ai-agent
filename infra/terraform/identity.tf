# Amazon Cognito issues the access tokens the API verifies (ADR-0009, ADR-0011):
# - users are created by administrators only (no self sign-up);
# - the role is a Cognito group (claim cognito:groups), the tenant an admin-only attribute
#   copied into the access token by the pre token generation trigger (claim tenant_id);
# - the app client cannot write the tenant attribute, so a user cannot change tenants.

locals {
  roles = toset(["support_agent", "finance_analyst", "supervisor", "editor"])
}

resource "aws_cognito_user_pool" "main" {
  name                = local.name
  user_pool_tier      = "ESSENTIALS" # access token customization
  deletion_protection = "INACTIVE"   # disposable environment
  mfa_configuration   = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  username_configuration {
    case_sensitive = false
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  schema {
    name                     = "tenant_id"
    attribute_data_type      = "String"
    mutable                  = true # by administrators only (see the app client)
    developer_only_attribute = false
    required                 = false

    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  lambda_config {
    pre_token_generation_config {
      lambda_arn     = aws_lambda_function.pre_token_generation.arn
      lambda_version = "V2_0"
    }
  }
}

resource "aws_cognito_user_group" "role" {
  for_each = local.roles

  name         = each.key
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Application role ${each.key}"
}

resource "aws_cognito_user_pool_client" "api" {
  name         = "${local.name}-api"
  user_pool_id = aws_cognito_user_pool.main.id

  generate_secret = false
  # SRP for people; the admin flow lets an operator with IAM credentials sign in demo users.
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  auth_session_validity         = 3

  access_token_validity  = 30
  id_token_validity      = 30
  refresh_token_validity = 8
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }

  # Explicit lists: left unset, Cognito lets users write every attribute, tenant included.
  read_attributes  = ["name", "custom:tenant_id"]
  write_attributes = ["name"]
}

# --- Pre token generation trigger ---

data "archive_file" "pre_token_generation" {
  type        = "zip"
  source_file = "${path.module}/lambda/pre_token_generation.py"
  output_path = "${path.module}/.build/pre_token_generation.zip"
}

resource "aws_lambda_function" "pre_token_generation" {
  function_name    = "${local.name}-pre-token-generation"
  role             = aws_iam_role.pre_token_generation.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "pre_token_generation.handler"
  filename         = data.archive_file.pre_token_generation.output_path
  source_code_hash = data.archive_file.pre_token_generation.output_base64sha256
  memory_size      = 128
  timeout          = 3

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.pre_token_generation.name
  }
}

resource "aws_cloudwatch_log_group" "pre_token_generation" {
  name              = "/aws/lambda/${local.name}-pre-token-generation"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_lambda_permission" "cognito" {
  statement_id   = "AllowCognitoInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.pre_token_generation.function_name
  principal      = "cognito-idp.amazonaws.com"
  source_arn     = aws_cognito_user_pool.main.arn
  source_account = local.account_id
}

resource "aws_iam_role" "pre_token_generation" {
  name               = "${local.name}-pre-token-generation"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "pre_token_generation" {
  name = "logs-and-traces"
  role = aws_iam_role.pre_token_generation.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "OwnLogGroup"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.pre_token_generation.arn}:*"
      },
      {
        Sid      = "XRayTraces"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
    ]
  })
}
