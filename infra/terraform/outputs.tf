output "api_url" {
  description = "Base URL of the API (HTTPS)."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "cognito_user_pool_id" {
  description = "User pool that issues the API's access tokens."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "App client ID (the token audience)."
  value       = aws_cognito_user_pool_client.api.id
}

output "cognito_jwks_url" {
  description = "Token signing keys."
  value       = local.jwks_url
}

output "ecr_repository_url" {
  description = "Registry for the runtime and ops images."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.main.name
}

output "ops_task_definition" {
  description = "Task definition family for migrations and seeding (aws ecs run-task)."
  value       = local.deploy_services ? aws_ecs_task_definition.ops[0].family : null
}

output "ops_network_configuration" {
  description = "awsvpcConfiguration for one-off tasks."
  value = {
    subnets        = aws_subnet.private[*].id
    securityGroups = [aws_security_group.ops.id]
    assignPublicIp = "DISABLED"
  }
}

output "documents_bucket" {
  description = "Bucket for uploaded PDFs."
  value       = aws_s3_bucket.documents.bucket
}

output "ingestion_queue" {
  description = "Ingestion queue name."
  value       = aws_sqs_queue.ingestion.name
}

output "ci_image_push_role_arn" {
  description = "Role GitHub Actions assumes through OIDC (repository variable AWS_ROLE_ARN)."
  value       = local.github_ci ? aws_iam_role.ci_image_push[0].arn : null
}
