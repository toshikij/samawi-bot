locals {
  name     = "samawi"
  schedule = jsondecode(file("${path.module}/schedule.json"))
  # comment→DM runs every 5 minutes, so it only exists once the app is live.
  jobs = { for k, v in local.schedule : k => v if !try(v.live_only, false) || var.launch_mode == "live" }
  secret_keys = [
    "ANTHROPIC_API_KEY", "IG_USER_ID", "IG_ACCESS_TOKEN", "THREADS_USER_ID", "THREADS_ACCESS_TOKEN",
    "META_APP_ID", "META_APP_SECRET", "SESSION_SECRET", "SAMAWI_REPO_TOKEN",
  ]
  task_def_family_arn = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/samawi-pipeline"
  common_env = [
    { name = "TZ", value = "UTC" },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "EMAIL_TRANSPORT", value = "ses" },
    { name = "ALERT_EMAIL", value = var.alert_email },
    { name = "ALERT_FROM", value = "Samawi Bot <${var.alert_from_address}>" },
    { name = "S3_BUCKET", value = aws_s3_bucket.assets.bucket },
    { name = "SNAPSHOT_DIR", value = "/data/snapshot" },
    { name = "LAUNCH_MODE", value = var.launch_mode },
    { name = "PLAY_STORE_URL", value = var.play_store_url },
    { name = "TRANSLATION_MODE", value = var.translation_mode },
    { name = "KILL_SWITCH", value = tostring(var.kill_switch) },
    { name = "PUBLISH_LIVE", value = tostring(var.publish_live) },
    { name = "DASHBOARD_URL", value = "http://localhost:3200/samawi" },
    { name = "SCHEDULER_GROUP", value = aws_scheduler_schedule_group.publish.name },
    { name = "SCHEDULER_ROLE_ARN", value = aws_iam_role.scheduler.arn },
    { name = "ECS_CLUSTER_ARN", value = aws_ecs_cluster.main.arn },
    { name = "ECS_TASK_DEFINITION_ARN", value = local.task_def_family_arn },
    { name = "ECS_SUBNETS", value = join(",", var.subnet_ids) },
    { name = "ECS_SECURITY_GROUPS", value = aws_security_group.task.id },
  ]
  secrets = concat(
    [for k in local.secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }],
    [{ name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.db.arn}:DATABASE_URL::" }],
  )
}

data "aws_caller_identity" "current" {}

# ── Media: private bucket; Meta fetches each file through a presigned URL (6 h). ────────────────
resource "aws_s3_bucket" "assets" {
  bucket = "samawi-bot-assets-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id
  rule {
    id     = "expire-old-renders"
    status = "Enabled"
    filter { prefix = "posts/" }
    expiration { days = 400 }
  }
}

# ── Secrets: values are written by scripts, never by Terraform (they stay out of state). ───────
resource "aws_secretsmanager_secret" "app" {
  name        = "samawi-bot/app"
  description = "Samawi Bot env: ANTHROPIC_API_KEY, Meta tokens and IDs, SESSION_SECRET, SAMAWI_REPO_TOKEN"
}

resource "aws_ecr_repository" "app" {
  name                 = "samawi-bot"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "keep 15 images", selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 15 }, action = { type = "expire" } }] })
}

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/samawi-bot/pipeline"
  retention_in_days = 90
}

resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_security_group" "task" {
  name        = "samawi-bot-pipeline"
  description = "Outbound only: Claude, Meta, GitHub, HadeethEnc, SES, RDS"
  vpc_id      = var.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "samawi-bot-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.app.arn, aws_secretsmanager_secret.db.arn] }]
  })
}

resource "aws_iam_role" "task" {
  name               = "samawi-bot-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.assets.arn}/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.assets.arn },
      { Effect = "Allow", Action = ["ses:SendEmail"], Resource = "*", Condition = { StringEquals = { "ses:FromAddress" = var.alert_from_address } } },
      # produce creates one-off "publish this slot" schedules in its own group, and nothing else.
      { Effect = "Allow", Action = ["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:GetSchedule"], Resource = "arn:aws:scheduler:${var.aws_region}:${data.aws_caller_identity.current.account_id}:schedule/${aws_scheduler_schedule_group.publish.name}/*" },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.scheduler.arn },
    ]
  })
}

resource "aws_ecs_task_definition" "pipeline" {
  family                   = "samawi-pipeline"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 3072 # Chromium + ffmpeg for reels
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  ephemeral_storage { size_in_gib = 30 }
  container_definitions = jsonencode([{
    name             = "pipeline"
    image            = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential        = true
    command          = ["run", "healthcheck"]
    environment      = local.common_env
    secrets          = local.secrets
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.pipeline.name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "job" } }
  }])
}

# ── Scheduling: fixed jobs in WIB, plus a group for produce's one-off prayer-anchored publishes. ─
resource "aws_scheduler_schedule_group" "publish" {
  name = "samawi"
}

resource "aws_iam_role" "scheduler" {
  name = "samawi-bot-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecs:RunTask", "ecs:TagResource"], Resource = "*" },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn] },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.scheduler_dlq.arn },
    ]
  })
}

resource "aws_sqs_queue" "scheduler_dlq" {
  name                      = "samawi-bot-scheduler-dlq"
  message_retention_seconds = 1209600
}

resource "aws_scheduler_schedule" "job" {
  for_each                     = local.jobs
  name                         = "samawi-${each.key}"
  description                  = each.value.label
  schedule_expression          = each.value.cron
  schedule_expression_timezone = "Asia/Jakarta"
  state                        = var.schedules_enabled ? "ENABLED" : "DISABLED"
  start_date                   = var.schedules_start
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ containerOverrides = [{ name = "pipeline", command = ["run", each.key] }] })
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.pipeline.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = var.subnet_ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
      propagate_tags = "TASK_DEFINITION"
    }
    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 900
    }
    dead_letter_config { arn = aws_sqs_queue.scheduler_dlq.arn }
  }
}
