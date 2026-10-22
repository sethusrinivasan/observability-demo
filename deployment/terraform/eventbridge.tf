# 1. EventBridge 5-Minute Flush Safeguard for Collector Lambda
resource "aws_cloudwatch_event_rule" "collector_flush" {
  count               = var.enable_eventbridge_cron ? 1 : 0
  name                = "observability-collector-5min-flush"
  description         = "Triggers /flush on collector Lambda every 5 minutes to prevent stale telemetry in /tmp"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "collector_flush_target" {
  count     = var.enable_eventbridge_cron ? 1 : 0
  rule      = aws_cloudwatch_event_rule.collector_flush[0].name
  target_id = "CollectorFlushTarget"
  arn       = aws_lambda_function.collector.arn
  input     = jsonencode({ path = "/flush", httpMethod = "POST" })
}

resource "aws_lambda_permission" "eventbridge_flush" {
  count         = var.enable_eventbridge_cron ? 1 : 0
  statement_id  = "AllowExecutionFromEventBridgeFlush"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.collector_flush[0].arn
}

# 2. EventBridge 1-Minute Synthetic Canary Trigger
resource "aws_cloudwatch_event_rule" "canary_pulse" {
  count               = var.enable_eventbridge_cron ? 1 : 0
  name                = "observability-canary-1min-pulse"
  description         = "Triggers canary synthetic load pulse every 1 minute"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "canary_pulse_target" {
  count     = var.enable_eventbridge_cron ? 1 : 0
  rule      = aws_cloudwatch_event_rule.canary_pulse[0].name
  target_id = "CanaryPulseTarget"
  arn       = aws_lambda_function.canary.arn
  input     = jsonencode({ path = "/api/trigger-cycle", httpMethod = "POST" })
}

resource "aws_lambda_permission" "eventbridge_canary" {
  count         = var.enable_eventbridge_cron ? 1 : 0
  statement_id  = "AllowExecutionFromEventBridgeCanary"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.canary.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.canary_pulse[0].arn
}
