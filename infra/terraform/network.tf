# Private-only network: no internet gateway, no NAT gateway, no public IPs. Tasks reach AWS
# services through VPC endpoints whose policies only admit this account's principals and
# resources, so a compromised task (or a manipulated agent) has no path to send data to
# the internet or to another AWS account. Callers come in through API Gateway's VPC link.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # required by private DNS on interface endpoints

  tags = { Name = local.name }
}

resource "aws_subnet" "private" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

# Only the implicit local route: nothing in this VPC can reach the internet.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# The default security group allows all traffic between its members; strip every rule so
# nothing can use it by accident.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

# --- Flow logs: accepted and rejected connections, for investigation ---

resource "aws_flow_log" "vpc" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn         = aws_iam_role.flow_logs.arn
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/aws/vpc/${local.name}/flow-logs"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# --- Security groups (one per workload; rules reference groups, not CIDRs) ---

resource "aws_security_group" "vpc_link" {
  name        = "${local.name}-vpc-link"
  description = "API Gateway VPC link: forwards requests to the API tasks"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "api" {
  name        = "${local.name}-api"
  description = "API tasks"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "Ingestion worker tasks"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "ops" {
  name        = "${local.name}-ops"
  description = "One-off administrative tasks (migrations, seeding)"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "PostgreSQL"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-endpoints"
  description = "Interface VPC endpoints"
  vpc_id      = aws_vpc.main.id
}

locals {
  # Workloads that talk to the database, the endpoints and S3.
  workload_security_groups = {
    api    = aws_security_group.api.id
    worker = aws_security_group.worker.id
    ops    = aws_security_group.ops.id
  }
}

resource "aws_vpc_security_group_egress_rule" "vpc_link_to_api" {
  security_group_id            = aws_security_group.vpc_link.id
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  description                  = "Forward requests to the API tasks"
}

resource "aws_vpc_security_group_ingress_rule" "api_from_vpc_link" {
  security_group_id            = aws_security_group.api.id
  referenced_security_group_id = aws_security_group.vpc_link.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  description                  = "Requests from API Gateway"
}

resource "aws_vpc_security_group_egress_rule" "workload_to_database" {
  for_each = local.workload_security_groups

  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.database.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL"
}

resource "aws_vpc_security_group_ingress_rule" "database_from_workload" {
  for_each = local.workload_security_groups

  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "PostgreSQL from ${each.key} tasks"
}

resource "aws_vpc_security_group_egress_rule" "workload_to_endpoints" {
  for_each = local.workload_security_groups

  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "AWS APIs through interface endpoints"
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_workload" {
  for_each = local.workload_security_groups

  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "HTTPS from ${each.key} tasks"
}

resource "aws_vpc_security_group_egress_rule" "workload_to_s3" {
  for_each = local.workload_security_groups

  security_group_id = each.value
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "S3 through the gateway endpoint (documents, image layers)"
}

# --- VPC endpoints ---

locals {
  # Bedrock's endpoint only when a model provider uses it; Cognito's only to serve the
  # JWKS when the keys are not given inline. Each interface endpoint is billed by the hour.
  uses_bedrock = var.llm_provider == "bedrock" || var.embedding_provider == "bedrock"
  interface_endpoints = toset(concat(
    ["ecr.api", "ecr.dkr", "logs", "secretsmanager", "sqs"],
    local.uses_bedrock ? ["bedrock-runtime"] : [],
    var.auth_jwks_json == null ? ["cognito-idp"] : [],
  ))

  # Endpoints whose resources all live in this account: callers *and* resources must
  # belong to it. Bedrock's foundation models are AWS-owned, so only the caller is checked;
  # ECR's GetAuthorizationToken has no resource, likewise. Cognito's JWKS is anonymous.
  same_account_endpoints = toset(["logs", "secretsmanager", "sqs"])
  caller_only_endpoints  = toset(["ecr.api", "ecr.dkr", "bedrock-runtime"])
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Interface"
  # One subnet: endpoints are billed per AZ-hour, and staging accepts losing one AZ.
  subnet_ids          = [aws_subnet.private[0].id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  policy = (
    contains(local.same_account_endpoints, each.key) ? data.aws_iam_policy_document.endpoint_same_account.json :
    contains(local.caller_only_endpoints, each.key) ? data.aws_iam_policy_document.endpoint_same_caller.json :
    null
  )

  tags = { Name = "${local.name}-${each.key}" }
}

data "aws_iam_policy_document" "endpoint_same_account" {
  statement {
    sid       = "ThisAccountOnly"
    actions   = ["*"]
    resources = ["*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [local.account_id]
    }
  }
}

data "aws_iam_policy_document" "endpoint_same_caller" {
  statement {
    sid       = "ThisAccountCallersOnly"
    actions   = ["*"]
    resources = ["*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy            = data.aws_iam_policy_document.endpoint_s3.json

  tags = { Name = "${local.name}-s3" }
}

# S3 through the VPC: this project's bucket, and ECR's image layers. No other bucket.
data "aws_iam_policy_document" "endpoint_s3" {
  statement {
    sid       = "DocumentsBucket"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "EcrImageLayers"
    actions   = ["s3:GetObject"]
    resources = ["arn:${local.partition}:s3:::prod-${var.region}-starport-layer-bucket/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}
