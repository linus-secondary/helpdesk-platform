output "region" {
  value = var.region
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "database_secret_name" {
  description = "Secrets Manager secret that holds DATABASE_URL"
  value       = aws_secretsmanager_secret.db.name
}

output "chatbot_role_arn" {
  value = aws_iam_role.chatbot.arn
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "notifications_role_arn" {
  value = aws_iam_role.notifications.arn
}

output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "waf_acl_arn" {
  value = aws_wafv2_web_acl.main.arn
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}
