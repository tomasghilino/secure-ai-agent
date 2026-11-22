# Least privilege, one role per workload. Every statement names this project's resources;
# the only wildcards are actions AWS defines without a resource (ECR authorization tokens).

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    # Confused deputy protection: only ECS acting for this account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:*"]
    }
  }
}

# --- Execution role: what the ECS agent needs to start a task ---

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "execution" {
  name   = "start-tasks"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "RegistryAuthentication"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action has no resource
  }
  statement {
    sid       = "PullImages"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for group in aws_cloudwatch_log_group.ecs : "${group.arn}:*"]
  }
  statement {
    sid       = "DatabaseSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.main.master_user_secret[0].secret_arn]
  }
  statement {
    sid       = "DecryptSecretAndImages"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com", "ecr.${var.region}.amazonaws.com"]
    }
  }
}

# --- Task roles: what the application code may do ---

resource "aws_iam_role" "task" {
  for_each = toset(["api", "worker"])

  name               = "${local.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "api" {
  name   = "api"
  role   = aws_iam_role.task["api"].id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role_policy" "worker" {
  name   = "worker"
  role   = aws_iam_role.task["worker"].id
  policy = data.aws_iam_policy_document.worker.json
}

locals {
  uploads_prefix = "uploads/"

  # A cross-Region inference profile (us., eu., apac., global.) routes to foundation models
  # in several Regions; the model ARNs are only usable through that profile (condition).
  chat_model_is_profile = can(regex("^(us|eu|apac|global)\\.", var.bedrock_chat_model_id))
  chat_base_model_id    = local.chat_model_is_profile ? join(".", slice(split(".", var.bedrock_chat_model_id), 1, length(split(".", var.bedrock_chat_model_id)))) : var.bedrock_chat_model_id
  chat_profile_arn      = "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${var.bedrock_chat_model_id}"
  embedding_model_arn   = "arn:${local.partition}:bedrock:${var.region}::foundation-model/${var.bedrock_embedding_model_id}"
}

data "aws_iam_policy_document" "embeddings" {
  statement {
    sid       = "EmbeddingModel"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.embedding_model_arn]
  }
}

data "aws_iam_policy_document" "chat" {
  dynamic "statement" {
    for_each = local.chat_model_is_profile ? [1] : []
    content {
      sid       = "ChatInferenceProfile"
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = [local.chat_profile_arn]
    }
  }
  dynamic "statement" {
    for_each = local.chat_model_is_profile ? [1] : []
    content {
      sid     = "ChatModelThroughTheProfileOnly"
      actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = [
        "arn:${local.partition}:bedrock:*::foundation-model/${local.chat_base_model_id}*",
        "arn:${local.partition}:bedrock:::foundation-model/${local.chat_base_model_id}*",
      ]
      condition {
        test     = "StringEquals"
        variable = "bedrock:InferenceProfileArn"
        values   = [local.chat_profile_arn]
      }
    }
  }
  dynamic "statement" {
    for_each = local.chat_model_is_profile ? [] : [1]
    content {
      sid       = "ChatModel"
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = ["arn:${local.partition}:bedrock:${var.region}::foundation-model/${var.bedrock_chat_model_id}"]
    }
  }
}

data "aws_iam_policy_document" "api" {
  source_policy_documents = [
    data.aws_iam_policy_document.embeddings.json, # query embeddings
    data.aws_iam_policy_document.chat.json,       # answers and the agent
  ]

  statement {
    sid       = "EnqueueIngestionJobs"
    actions   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
    resources = [aws_sqs_queue.ingestion.arn]
  }
  statement {
    sid       = "StoreUploads"
    actions   = ["s3:PutObject"] # the API only writes; the worker reads
    resources = ["${aws_s3_bucket.documents.arn}/${local.uploads_prefix}*"]
  }
  statement {
    sid       = "EncryptUploadsAndJobs"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com", "sqs.${var.region}.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "worker" {
  source_policy_documents = [data.aws_iam_policy_document.embeddings.json]

  statement {
    sid = "ConsumeIngestionJobs"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueUrl",
    ]
    resources = [aws_sqs_queue.ingestion.arn]
  }
  statement {
    sid       = "ReadUploads"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/${local.uploads_prefix}*"]
  }
  # Without ListBucket, a missing object reads as AccessDenied instead of NoSuchKey.
  statement {
    sid       = "TellMissingUploadsApart"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.uploads_prefix}*"]
    }
  }
  statement {
    sid       = "DecryptUploadsAndJobs"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com", "sqs.${var.region}.amazonaws.com"]
    }
  }
}

# --- VPC flow logs ---

resource "aws_iam_role" "flow_logs" {
  name = "${local.name}-flow-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${local.partition}:ec2:${var.region}:${local.account_id}:vpc-flow-log/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "write-flow-logs"
  role = aws_iam_role.flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow_logs.arn}:*"
    }]
  })
}
