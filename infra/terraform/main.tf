data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

locals {
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
  name = "amrutam-${var.environment}"
}

resource "aws_ecr_repository" "api" {
  name                 = "amrutam-telemedicine-api"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the most recent 50 release images"
      selection = {
        tagStatus     = "tagged"
        tagPrefixList = ["sha-"]
        countType     = "imageCountMoreThan"
        countNumber   = 50
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_iam_role" "github_release" {
  name = "${local.name}-github-release"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = data.aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:ref:refs/heads/master"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_release" {
  name = "publish-and-deploy-api"
  role = aws_iam_role.github_release.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ]
        Resource = aws_ecr_repository.api.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices",
          "ecs:DescribeTaskDefinition",
          "ecs:DescribeTasks",
          "ecs:RegisterTaskDefinition",
          "ecs:RunTask",
          "ecs:UpdateService"
        ]
        Resource = [
          aws_ecs_cluster.main.arn,
          aws_ecs_service.api.id,
          "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.name}-api:*",
          "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.name}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.task_execution.arn]
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  map_public_ip_on_launch = false
}

resource "aws_subnet" "application" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
}

resource "aws_subnet" "data" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 20)
}

resource "aws_eip" "nat" {
  count  = length(local.azs)
  domain = "vpc"
}

resource "aws_nat_gateway" "main" {
  count         = length(local.azs)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "application" {
  count  = length(local.azs)
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[count.index].id
  }
}

resource "aws_route_table_association" "application" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.application[count.index].id
  route_table_id = aws_route_table.application[count.index].id
}

resource "aws_route_table" "data" {
  count  = length(local.azs)
  vpc_id = aws_vpc.main.id
}

resource "aws_route_table_association" "data" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data[count.index].id
}

resource "aws_security_group" "load_balancer" {
  name   = "${local.name}-alb"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "application" {
  name   = "${local.name}-api"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.load_balancer.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "database" {
  name   = "${local.name}-postgres"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.application.id]
  }
}

resource "aws_security_group" "cache" {
  name   = "${local.name}-redis"
  vpc_id = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.application.id]
  }
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.data[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-postgres"
  family = "postgres16"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }
}

resource "random_password" "database" {
  length  = 40
  special = false
}

resource "random_password" "redis" {
  length  = 40
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "encryption" {
  length  = 64
  special = false
}

resource "random_password" "webhook" {
  length  = 64
  special = false
}

resource "aws_db_instance" "main" {
  identifier                      = "${local.name}-postgres"
  engine                          = "postgres"
  instance_class                  = "db.r6g.large"
  allocated_storage               = 100
  max_allocated_storage           = 1000
  storage_encrypted               = true
  db_name                         = "amrutam"
  username                        = "amrutam_app"
  password                        = random_password.database.result
  port                            = 5432
  db_subnet_group_name            = aws_db_subnet_group.main.name
  parameter_group_name            = aws_db_parameter_group.main.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  multi_az                        = true
  publicly_accessible             = false
  backup_retention_period         = 14
  deletion_protection             = true
  copy_tags_to_snapshot           = true
  auto_minor_version_upgrade      = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${local.name}-final"
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.data[*].id
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id       = "${var.environment}-amrutam"
  description                = "Rate limiting and ephemeral cache"
  engine                     = "redis"
  node_type                  = "cache.r7g.large"
  port                       = 6379
  parameter_group_name       = "default.redis7"
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
  snapshot_retention_limit   = 7
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.cache.id]
}

resource "aws_secretsmanager_secret" "application" {
  name                    = "${local.name}/application"
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "application" {
  secret_id = aws_secretsmanager_secret.application.id
  secret_string = jsonencode({
    DATABASE_URL           = "postgresql+psycopg://amrutam_app:${random_password.database.result}@${aws_db_instance.main.address}:5432/amrutam?sslmode=require"
    REDIS_URL              = "rediss://:${random_password.redis.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
    JWT_SECRET             = random_password.jwt.result
    DATA_ENCRYPTION_KEY    = random_password.encryption.result
    PAYMENT_WEBHOOK_SECRET = random_password.webhook.result
  })
}

resource "aws_cloudwatch_log_group" "application" {
  name              = "/ecs/${local.name}"
  retention_in_days = 90
}

resource "aws_iam_role" "task_execution" {
  name = "${local.name}-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "read_application_secret" {
  name = "read-application-secret"
  role = aws_iam_role.task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.application.arn]
    }]
  })
}

resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.task_execution.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = var.container_image
    essential = true
    user      = "10001"
    portMappings = [{
      containerPort = 8000
      protocol      = "tcp"
    }]
    environment = [
      { name = "APP_ENV", value = "production" },
      { name = "TRUSTED_HOSTS", value = var.app_host },
      { name = "JWT_ISSUER", value = "amrutam-api" },
      { name = "OTEL_SERVICE_NAME", value = "amrutam-telemedicine-api" }
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.application.arn}:DATABASE_URL::" },
      { name = "REDIS_URL", valueFrom = "${aws_secretsmanager_secret.application.arn}:REDIS_URL::" },
      { name = "JWT_SECRET", valueFrom = "${aws_secretsmanager_secret.application.arn}:JWT_SECRET::" },
      { name = "DATA_ENCRYPTION_KEY", valueFrom = "${aws_secretsmanager_secret.application.arn}:DATA_ENCRYPTION_KEY::" },
      { name = "PAYMENT_WEBHOOK_SECRET", valueFrom = "${aws_secretsmanager_secret.application.arn}:PAYMENT_WEBHOOK_SECRET::" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.application.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "api"
      }
    }
    healthCheck = {
      command     = ["CMD", "python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }
    readonlyRootFilesystem = true
    linuxParameters = {
      initProcessEnabled = true
      tmpfs = [{
        containerPath = "/tmp"
        size          = 64
        mountOptions  = ["noexec", "nosuid"]
      }]
    }
  }])
}

resource "aws_lb" "api" {
  name               = "${var.environment}-amrutam-api"
  internal           = false
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.load_balancer.id]
}

resource "aws_lb_target_group" "api" {
  name        = "${var.environment}-amrutam-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/health/ready"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.application[*].id
    security_groups  = [aws_security_group.application.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.https, aws_iam_role_policy.read_application_secret]
}

resource "aws_appautoscaling_target" "api" {
  min_capacity       = var.minimum_count
  max_capacity       = var.maximum_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 180
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
