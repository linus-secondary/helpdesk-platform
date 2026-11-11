# ------------------------------------------------------------------ PostgreSQL (Amazon RDS)
resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL reachable only from inside the VPC (EKS nodes)"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description = "PostgreSQL from the VPC"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true
  kms_key_id        = aws_kms_key.main.arn
  multi_az          = var.db_multi_az

  db_name  = "helpdesk"
  username = "helpdeskadmin"
  password = random_password.db.result

  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false

  backup_retention_period         = 7
  copy_tags_to_snapshot           = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true
  deletion_protection             = false # demo project: allow terraform destroy
  skip_final_snapshot             = true
}

# The connection string is kept in AWS Secrets Manager, never in Git.
resource "aws_secretsmanager_secret" "db" {
  name                    = "${local.name}/database-url"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id     = aws_secretsmanager_secret.db.id
  secret_string = "postgresql://helpdeskadmin:${random_password.db.result}@${aws_db_instance.main.address}:5432/helpdesk?sslmode=require"
}

# ------------------------------------------------------------------ container registry
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true # free basic vulnerability scan on every push
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
      description  = "Keep only the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
