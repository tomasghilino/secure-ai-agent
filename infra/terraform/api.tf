# Entry point: an API Gateway HTTP API. It gives HTTPS without owning a domain, verifies
# the Cognito token before a request enters the VPC (the API verifies it again), throttles
# all callers, writes access logs, and reaches the private API tasks through a VPC link.
# Only /v1/* and /health/* are routed; anything else is a 404 at the edge.

resource "aws_apigatewayv2_api" "main" {
  name          = local.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_vpc_link" "main" {
  name               = local.name
  subnet_ids         = aws_subnet.private[*].id
  security_group_ids = [aws_security_group.vpc_link.id]
}

resource "aws_apigatewayv2_integration" "api" {
  api_id               = aws_apigatewayv2_api.main.id
  integration_type     = "HTTP_PROXY"
  integration_method   = "ANY"
  connection_type      = "VPC_LINK"
  connection_id        = aws_apigatewayv2_vpc_link.main.id
  integration_uri      = aws_service_discovery_service.api.arn
  timeout_milliseconds = 30000 # the HTTP API maximum

  # The gateway's request ID replaces any client value, so logs at both layers correlate
  # and a caller cannot choose the ID written to the application logs.
  request_parameters = {
    "overwrite:header.X-Request-ID" = "$context.requestId"
  }
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.main.id
  name             = "cognito"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    issuer   = "https://${aws_cognito_user_pool.main.endpoint}"
    audience = [aws_cognito_user_pool_client.api.id] # matched against client_id
  }
}

resource "aws_apigatewayv2_route" "v1" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "ANY /v1/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_apigatewayv2_route" "health" {
  api_id             = aws_apigatewayv2_api.main.id
  route_key          = "GET /health/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.api_throttling_rate_limit
    throttling_burst_limit = var.api_throttling_burst_limit
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId          = "$context.requestId"
      requestTime        = "$context.requestTime"
      sourceIp           = "$context.identity.sourceIp"
      httpMethod         = "$context.httpMethod"
      routeKey           = "$context.routeKey"
      status             = "$context.status"
      responseLength     = "$context.responseLength"
      integrationLatency = "$context.integrationLatency"
      integrationError   = "$context.integrationErrorMessage"
      authorizerError    = "$context.authorizer.error"
      subject            = "$context.authorizer.claims.sub"
    })
  }
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${local.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}
