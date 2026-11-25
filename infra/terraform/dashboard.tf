# The dashboard: one small Fargate task, owner only. No inbound access at all; the way in is an ECS
# Exec port-forward (scripts/dashboard-tunnel.sh, needs AWS credentials), then the emailed sign-in link.
resource "aws_security_group" "dashboard" {
  name        = "samawi-bot-dashboard"
  description = "Dashboard: no inbound; reached by SSM port-forward only"
  vpc_id      = var.vpc_id
  ingress     = []
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_iam_role" "dashboard" {
  name               = "samawi-bot-ecs-dashboard"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "dashboard" {
  role = aws_iam_role.dashboard.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.assets.arn}/*" },
      { Effect = "Allow", Action = ["ses:SendEmail"], Resource = "*", Condition = { StringEquals = { "ses:FromAddress" = var.alert_from_address } } },
      { Effect = "Allow", Action = ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"], Resource = "*" },
    ]
  })
}

resource "aws_ecs_task_definition" "dashboard" {
  family                   = "samawi-dashboard"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.dashboard.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name         = "dashboard"
    image        = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential    = true
    command      = ["admin", "--port", "3200"]
    portMappings = [{ containerPort = 3200, protocol = "tcp" }]
    environment  = local.common_env
    secrets      = local.secrets
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3200/samawi/healthz').status == 200 else 1)"]
      interval    = 30
      timeout     = 10
      retries     = 3
      startPeriod = 60
    }
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.pipeline.name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "dashboard" } }
  }])
}

resource "aws_ecs_service" "dashboard" {
  name                               = "samawi-dashboard"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.dashboard.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.dashboard.id]
    assign_public_ip = true # outbound only; the security group admits nothing
  }
  enable_execute_command = true
  propagate_tags         = "SERVICE"
}
