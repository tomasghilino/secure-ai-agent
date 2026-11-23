# --- Account and naming ---

variable "allowed_account_id" {
  description = "The only AWS account this configuration may touch (set in terraform.tfvars, never committed)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.allowed_account_id))
    error_message = "allowed_account_id must be a 12-digit AWS account ID."
  }
}

variable "aws_profile" {
  description = "AWS CLI profile (an IAM Identity Center profile); null uses the default credential chain."
  type        = string
  default     = null
}

variable "region" {
  description = "AWS Region for every resource."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project tag on every resource (cost attribution and cleanup audits)."
  type        = string
  default     = "northstar-ai-operations-copilot"
}

variable "environment" {
  description = "Environment name, used in resource names and tags."
  type        = string
  default     = "staging"
}

variable "repository_tag" {
  description = "Source repository, recorded as a tag."
  type        = string
  default     = "secure-ai-agent"
}

# --- Network ---

variable "vpc_cidr" {
  description = "CIDR block of the VPC. Two private /24 subnets are carved from it."
  type        = string
  default     = "10.40.0.0/16"
}

# --- Application ---

variable "image_tag" {
  description = "Image tag to run (immutable; the Git commit). Null creates everything except the ECS services and task definitions, so the registry exists before the first push."
  type        = string
  default     = null
}

variable "llm_provider" {
  description = "Chat model provider for the agent: bedrock, or fake to exercise the infrastructure without model access."
  type        = string
  default     = "bedrock"

  validation {
    condition     = contains(["bedrock", "fake"], var.llm_provider)
    error_message = "llm_provider must be bedrock or fake."
  }
}

variable "embedding_provider" {
  description = "Embedding provider: bedrock, or fake."
  type        = string
  default     = "bedrock"

  validation {
    condition     = contains(["bedrock", "fake"], var.embedding_provider)
    error_message = "embedding_provider must be bedrock or fake."
  }
}

variable "bedrock_chat_model_id" {
  description = "Bedrock chat model or inference profile ID (verify availability and quotas in the account first)."
  type        = string
  default     = "us.anthropic.claude-haiku-5-5"
}

variable "bedrock_embedding_model_id" {
  description = "Bedrock embedding model ID."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "agent_reference_date" {
  description = "\"Today\" for the agent; the seeded business data is written against this date."
  type        = string
  default     = "2026-11-15"
}

variable "auth_jwks_json" {
  description = "Cognito's JWKS document, only if the VPC endpoint does not serve the JWKS URL. Null: the API fetches the keys through the cognito-idp endpoint."
  type        = string
  default     = null
}

# --- Capacity and limits ---

variable "api_desired_count" {
  description = "Running API tasks."
  type        = number
  default     = 1
}

variable "worker_desired_count" {
  description = "Running ingestion workers."
  type        = number
  default     = 1
}

variable "api_throttling_rate_limit" {
  description = "Requests per second accepted by the API gateway (steady state), across all callers."
  type        = number
  default     = 10
}

variable "api_throttling_burst_limit" {
  description = "Burst of requests accepted by the API gateway."
  type        = number
  default     = 20
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 7
}

# --- CI/CD (GitHub Actions through OIDC; optional) ---

variable "github_repository" {
  description = "owner/name of the GitHub repository allowed to push images through OIDC. Null: no CI role."
  type        = string
  default     = null
}

variable "create_github_oidc_provider" {
  description = "Create the account's GitHub OIDC provider (false if the account already has one)."
  type        = bool
  default     = true
}
