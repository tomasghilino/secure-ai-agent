# ECS on Fargate: the API, the ingestion worker and one-off administrative tasks, all from
# images in this account's registry, in private subnets, as a non-root user with a
# read-only root filesystem and no Linux capabilities.

# Accepted for staging: Container Insights is billed per metric; logs cover it.
#trivy:ignore:AWS-0034
resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # cost; logs and flow logs cover staging
  }
}

resource "aws_cloudwatch_log_group" "ecs" {
  for_each = toset(["api", "worker", "ops"])

  name              = "/aws/ecs/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# Service discovery: API Gateway's VPC link finds the API tasks through Cloud Map.
resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${local.name}.internal"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "api" {
  name = "api"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "SRV" # carries the port, which API Gateway needs
      ttl  = 10
    }
  }
}

locals {
  repository_url = aws_ecr_repository.app.repository_url
  jwks_url       = "https://${aws_cognito_user_pool.main.endpoint}/.well-known/jwks.json"

  environment = [for name, value in merge(
    {
      APP_ENV                    = "staging"
      LOG_FORMAT                 = "json"
      LOG_LEVEL                  = "INFO"
      AWS_REGION                 = var.region
      DATABASE_URL               = "postgresql+psycopg://${aws_db_instance.main.username}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${aws_db_instance.main.db_name}?sslmode=require"
      LLM_PROVIDER               = var.llm_provider
      EMBEDDING_PROVIDER         = var.embedding_provider
      BEDROCK_CHAT_MODEL_ID      = var.bedrock_chat_model_id
      BEDROCK_EMBEDDING_MODEL_ID = var.bedrock_embedding_model_id
      AGENT_REFERENCE_DATE       = var.agent_reference_date
      STORAGE_BACKEND            = "s3"
      STORAGE_BUCKET             = aws_s3_bucket.documents.bucket
      INGESTION_QUEUE_NAME       = aws_sqs_queue.ingestion.name
      INGESTION_MAX_RECEIVES     = "3"
      MAX_UPLOAD_BYTES           = "10485760" # API Gateway's 10 MB payload limit
      AUTH_MODE                  = "jwt"
      AUTH_JWT_ISSUER            = "https://${aws_cognito_user_pool.main.endpoint}"
      AUTH_JWT_AUDIENCE          = aws_cognito_user_pool_client.api.id
      AUTH_AUDIENCE_CLAIM        = "client_id"
      AUTH_TENANT_CLAIM          = "tenant_id"
      AUTH_ROLES_CLAIM           = "cognito:groups"
    },
    var.auth_jwks_json == null ? { AUTH_JWKS_URL = local.jwks_url } : { AUTH_JWKS_JSON = var.auth_jwks_json },
  ) : { name = name, value = value }]

  # Injected by the ECS agent from Secrets Manager at start; never in the task definition.
  secrets = [{
    name      = "DATABASE_PASSWORD"
    valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::"
  }]

  # Empty lists are written out because ECS stores them: otherwise every plan would
  # show a difference and replace the task definitions.
  hardening = {
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    privileged             = false
    linuxParameters = {
      capabilities       = { add = [], drop = ["ALL"] }
      initProcessEnabled = true
    }
    systemControls = []
    volumesFrom    = []
    # Fargate has no tmpfs: /tmp is an ephemeral task volume (the root stays read-only).
    mountPoints = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
  }
}

resource "aws_ecs_task_definition" "api" {
  count = local.deploy_services ? 1 : 0

  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task["api"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.hardening, {
    name         = "api"
    image        = "${local.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ name = "http", containerPort = 8000, protocol = "tcp" }]
    environment  = local.environment
    secrets      = local.secrets
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.ecs["api"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  })])
}

resource "aws_ecs_task_definition" "worker" {
  count = local.deploy_services ? 1 : 0

  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task["worker"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.hardening, {
    name         = "worker"
    image        = "${local.repository_url}:${var.image_tag}"
    essential    = true
    command      = ["python", "-m", "app.ingestion.worker"]
    portMappings = []
    environment  = local.environment
    secrets      = local.secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.ecs["worker"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  })])
}

# One-off tasks (`aws ecs run-task`): migrations by default, seeding with a command
# override. No task role: these tasks only talk to the database.
resource "aws_ecs_task_definition" "ops" {
  count = local.deploy_services ? 1 : 0

  family                   = "${local.name}-ops"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.hardening, {
    name         = "ops"
    image        = "${local.repository_url}:ops-${var.image_tag}"
    essential    = true
    command      = ["alembic", "upgrade", "head"]
    portMappings = []
    environment  = local.environment
    secrets      = local.secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.ecs["ops"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "ops"
      }
    }
  })])
}

resource "aws_ecs_service" "api" {
  count = local.deploy_services ? 1 : 0

  name             = "api"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.api[0].arn
  desired_count    = var.api_desired_count
  launch_type      = "FARGATE"
  platform_version = "1.4.0"
  propagate_tags   = "SERVICE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn   = aws_service_discovery_service.api.arn
    container_name = "api"
    container_port = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Endpoints must exist before tasks pull images or read secrets.
  depends_on = [aws_vpc_endpoint.interface, aws_vpc_endpoint.s3]
}

resource "aws_ecs_service" "worker" {
  count = local.deploy_services ? 1 : 0

  name             = "worker"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.worker[0].arn
  desired_count    = var.worker_desired_count
  launch_type      = "FARGATE"
  platform_version = "1.4.0"
  propagate_tags   = "SERVICE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_vpc_endpoint.interface, aws_vpc_endpoint.s3]
}
