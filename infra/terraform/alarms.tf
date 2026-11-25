# Independent of the app's own SES alerts: if the app cannot run, cannot reach its database or cannot
# send email, these still reach you, through SNS.

resource "aws_sns_topic" "alerts" {
  name = "samawi-bot-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_policy" "alerts" {
  arn = aws_sns_topic.alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "cloudwatch.amazonaws.com"] }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alerts.arn
    }]
  })
}

# 1. A job task exited non-zero or was killed (crash, OOM, could not pull image, DB unreachable).
resource "aws_cloudwatch_event_rule" "task_failed" {
  name        = "samawi-bot-task-failed"
  state       = "ENABLED"
  description = "Samawi Bot pipeline task stopped with a non-zero exit"
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.main.arn]
      # Pipeline jobs only. The dashboard service replaces its task on every deploy, and the old one
      # exits non-zero on SIGTERM; that is not a failure. ECS restarts a crashed dashboard by itself.
      group      = ["family:samawi-pipeline"]
      lastStatus = ["STOPPED"]
      containers = { exitCode = [{ "anything-but" = 0 }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "task_failed" {
  rule      = aws_cloudwatch_event_rule.task_failed.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn
  input_transformer {
    input_paths    = { job = "$.detail.overrides.containerOverrides[0].command[1]", reason = "$.detail.stoppedReason", code = "$.detail.containers[0].exitCode" }
    input_template = "\"[SAMAWI] Task failed: <job> exited <code> (<reason>). Check /samawi-bot/pipeline logs and the dashboard.\""
  }
}

# 2. The scheduler could not start a task at all.
resource "aws_cloudwatch_metric_alarm" "scheduler_dlq" {
  alarm_name          = "samawi-bot-scheduler-could-not-start-job"
  alarm_description   = "EventBridge Scheduler failed to start a Samawi Bot job (see samawi-bot-scheduler-dlq)"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.scheduler_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  # Silent until the jobs are switched on, so an idle stack does not page you.
  actions_enabled = var.schedules_enabled
}

# 3. Dead man's switch: the watchdog logs a heartbeat every 30 minutes. No heartbeat for 2 hours means
#    nothing is running at all — not the scheduler, not the task, not the watchdog.
resource "aws_cloudwatch_log_metric_filter" "heartbeat" {
  name           = "samawi-bot-heartbeat"
  log_group_name = aws_cloudwatch_log_group.pipeline.name
  pattern        = "SAMAWI_HEARTBEAT"
  metric_transformation {
    name      = "Heartbeat"
    namespace = "SamawiBot"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "heartbeat" {
  alarm_name          = "samawi-bot-no-heartbeat"
  alarm_description   = "No Samawi Bot heartbeat for 3 hours: the pipeline is not running"
  namespace           = "SamawiBot"
  metric_name         = "Heartbeat"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  # Silent until the jobs are switched on, so an idle stack does not page you.
  actions_enabled = var.schedules_enabled
  ok_actions      = [aws_sns_topic.alerts.arn]
}

# 4. The app's own alert emails are failing (SES rejected them).
resource "aws_cloudwatch_log_metric_filter" "email_failed" {
  name           = "samawi-bot-email-send-failed"
  log_group_name = aws_cloudwatch_log_group.pipeline.name
  pattern        = "EMAIL_SEND_FAILED"
  metric_transformation {
    name      = "EmailSendFailed"
    namespace = "SamawiBot"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "email_failed" {
  alarm_name          = "samawi-bot-alert-email-failing"
  alarm_description   = "Samawi Bot could not send an alert email through SES"
  namespace           = "SamawiBot"
  metric_name         = "EmailSendFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  # Silent until the jobs are switched on, so an idle stack does not page you.
  actions_enabled = var.schedules_enabled
}
