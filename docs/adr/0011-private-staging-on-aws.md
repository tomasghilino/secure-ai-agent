# ADR-0011: A private, disposable staging environment on AWS

- Status: Accepted (not yet applied)
- Date: 2026-11-22

## Context

The application runs locally with Docker Compose. Staging must show the same system on AWS
with managed services (Bedrock, RDS, S3, SQS, Cognito), within a total project budget of
about USD 10, created for a demo and destroyed afterwards. It is also where the security
controls meet real infrastructure: identity, network boundaries, encryption, least
privilege. Constraints from earlier decisions: no NAT gateway, no OpenSearch, no Bedrock
Knowledge Bases, no provisioned throughput (ADR-0003, ADR-0004).

## Decision

1. **Terraform with local state** in `infra/terraform`, one root module. The provider
   refuses any account but the configured one (`allowed_account_ids`); account IDs and
   profile names live in an uncommitted `terraform.tfvars`. Provider versions are pinned
   and their checksums for Linux, Windows and macOS are in the committed lock file.
2. **No route to the internet.** Two private subnets, no internet gateway, no NAT, no
   public IPs. Tasks reach AWS through VPC endpoints (ECR, Logs, Secrets Manager, SQS,
   Bedrock Runtime, Cognito; S3 through a gateway endpoint). Endpoint policies form a data
   perimeter: only this account's principals, and for Logs, Secrets Manager and SQS only
   this account's resources; the S3 endpoint admits only the documents bucket and ECR's
   layer bucket. A compromised task or a manipulated agent has nowhere to send data.
3. **API Gateway HTTP API as the only entry point**, through a VPC link and Cloud Map to
   the API tasks: HTTPS without owning a domain, a JWT authorizer that rejects requests
   without a valid Cognito token before they enter the VPC, throttling for all callers,
   access logs, and only `/v1/*` and `/health/*` routed. The gateway's request ID
   overwrites the client's, so both log layers correlate.
4. **Amazon Cognito access tokens** (Essentials tier, inside the free tier for this use):
   administrators create users; the role is a Cognito group (`cognito:groups`); the tenant
   is a custom attribute the app client cannot write, copied into the access token by a
   pre token generation Lambda (`tenant_id`). The API keeps verifying tokens itself
   (ADR-0009), in `client_id` audience mode with `token_use=access`.
5. **ECS on Fargate** for the API, the ingestion worker and one-off tasks (migrations,
   seeding) from an `ops` image. Containers run as UID 10001 with a read-only root
   filesystem, all Linux capabilities dropped and a scratch volume for `/tmp`. Images are
   pulled by immutable tag from a private, KMS-encrypted registry with scan on push.
6. **Least-privilege IAM**: one role per workload, every statement on named resources. The
   API may enqueue jobs, write uploads and invoke the configured chat model only through
   its inference profile (an IAM condition) and the embedding model; the worker may consume
   jobs, read uploads and embed; one-off tasks have no task role at all. Trust policies
   carry `aws:SourceAccount` and `aws:SourceArn` conditions.
7. **Encryption and secrets**: one customer-managed KMS key with rotation for RDS, the
   database secret, S3, SQS, CloudWatch Logs and ECR. RDS generates and stores the master
   password in Secrets Manager; ECS injects it at start as `DATABASE_PASSWORD`, so it is
   never in Terraform state, variables or the task definition. TLS is enforced by RDS
   (`rds.force_ssl`), the bucket policy and the queue policies.
8. **CI pushes images through OIDC**: a role that trusts only this repository's jobs in the
   protected `staging` environment and can only push to this registry. No access keys.
9. **Two-phase deployment**: the first apply creates everything except the ECS services;
   images are pushed; a second apply with `image_tag` starts them.

## Alternatives considered

- **Application Load Balancer**: needs public subnets and an internet gateway, and HTTPS
  needs a domain and a certificate. API Gateway gives HTTPS, JWT checks and throttling at
  a lower cost for demo traffic.
- **Public subnets with public IPs instead of endpoints**: cheaper (no endpoint hours), but
  every task would have a path to the internet.
- **NAT gateway**: about USD 33 a month plus data, and the same internet path.
- **Cognito ID tokens**: they carry `aud` and custom attributes without a Lambda, but they
  are meant for the client application, include profile data and are not an API
  credential; the API refuses them.
- **Tenant from group names** (`tenant:nsi-amer`): no Lambda, but tenant and role would
  share one claim and one naming convention.
- **AWS-managed keys**: free, but a key policy cannot restrict who decrypts, and Trivy
  flags S3 and SQS without a customer-managed key as HIGH.

## Consequences

- Estimated cost while running: about USD 0.13 an hour (interface endpoints about half of
  it), so a four-hour demo costs under USD 1 plus Bedrock tokens. Left running for a month
  it would cost about USD 95: the runbook ends with `terraform destroy` and a tag-based
  check. See [docs/operations/staging.md](../operations/staging.md).
- Trivy reports no HIGH or CRITICAL misconfigurations; five lower findings are accepted
  for a disposable environment and annotated where they occur (backup retention, deletion
  protection, Performance Insights, Container Insights, bucket access logs).
- Residual risks: the API connects as the RDS master user (Row-Level Security still
  applies through `SET LOCAL ROLE`); TLS to RDS is `sslmode=require`, not `verify-full`;
  interface endpoints sit in one AZ; the HTTP API caps requests at 30 seconds and 10 MB;
  whether the Cognito endpoint serves the JWKS path privately is not documented by AWS
  and is the first deployment check (fallback: `auth_jwks_json`).
