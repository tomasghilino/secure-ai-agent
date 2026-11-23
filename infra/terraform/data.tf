# --- PostgreSQL 17 with pgvector (RDS, single-AZ: staging) ---

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-postgres17"
  family = "postgres17"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot" # static parameter; in effect from creation
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
}

# Accepted for a disposable environment: 1-day backups, no deletion protection (the
# database is destroyed with the environment) and no Performance Insights (cost).
#trivy:ignore:AWS-0077
#trivy:ignore:AWS-0177
#trivy:ignore:AWS-0133
resource "aws_db_instance" "main" {
  identifier     = "${local.name}-db"
  engine         = "postgres"
  engine_version = "17"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true
  kms_key_id        = aws_kms_key.main.arn

  db_name  = "northstar"
  username = "northstar_admin"
  # RDS generates the password and keeps it in Secrets Manager (rotated by RDS): it never
  # appears in Terraform state, variables or the task definition.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name
  publicly_accessible    = false
  multi_az               = false
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  iam_database_authentication_enabled = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  auto_minor_version_upgrade          = true
  backup_retention_period             = 1
  copy_tags_to_snapshot               = true

  # Disposable environment: destroyed with its data.
  deletion_protection = false
  skip_final_snapshot = true
  apply_immediately   = true

  depends_on = [aws_cloudwatch_log_group.database]
}

# Created here so it has a retention period and is removed by `terraform destroy`.
resource "aws_cloudwatch_log_group" "database" {
  name              = "/aws/rds/instance/${local.name}-db/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# --- Uploaded documents (S3) ---

# Accepted for staging: no server access logs. The bucket holds synthetic documents and
# is reachable only by two task roles, through the VPC endpoint policy.
#trivy:ignore:AWS-0089
resource "aws_s3_bucket" "documents" {
  bucket_prefix = "${local.name}-documents-"
  force_destroy = true # disposable environment
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    object_ownership = "BucketOwnerEnforced" # ACLs disabled
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_policy" "documents" {
  bucket = aws_s3_bucket.documents.id
  policy = data.aws_iam_policy_document.documents_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.documents]
}

data "aws_iam_policy_document" "documents_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

# --- Ingestion queue (SQS) with a dead-letter queue ---

resource "aws_sqs_queue" "ingestion_dlq" {
  name                              = "${local.name}-ingestion-dlq"
  message_retention_seconds         = 1209600 # 14 days, to inspect failed jobs
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300
}

resource "aws_sqs_queue" "ingestion" {
  name = "${local.name}-ingestion"
  # Longer than the worst-case processing of one document, or it is delivered twice.
  visibility_timeout_seconds        = 120
  receive_wait_time_seconds         = 10
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300

  # Must match INGESTION_MAX_RECEIVES (the worker fails the job on the last delivery).
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingestion_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "ingestion" {
  for_each = {
    ingestion = aws_sqs_queue.ingestion
    dlq       = aws_sqs_queue.ingestion_dlq
  }

  queue_url = each.value.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "sqs:*"
      Resource  = each.value.arn
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# --- Container registry ---

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE" # a tag always means the image that was scanned
  force_delete         = true        # disposable environment

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}
