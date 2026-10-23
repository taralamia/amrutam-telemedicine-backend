output "api_url" {
  value       = "https://${var.app_host}"
  description = "Create a DNS alias for this hostname to the ALB DNS name."
}

output "load_balancer_dns_name" {
  value       = aws_lb.api.dns_name
  description = "ALB DNS name to target from the app_host DNS record."
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.api.repository_url
  description = "Private ECR repository for CI-published immutable release images."
}

output "github_release_role_arn" {
  value       = aws_iam_role.github_release.arn
  description = "Configure as the AWS_ROLE_TO_ASSUME GitHub Actions repository variable."
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  value = aws_ecs_service.api.name
}

output "ecs_task_family" {
  value = aws_ecs_task_definition.api.family
}

output "application_subnet_ids" {
  value       = aws_subnet.application[*].id
  description = "Private subnet IDs for the one-shot migration task in CI."
}

output "application_security_group_id" {
  value       = aws_security_group.application.id
  description = "Application task security group ID for CI migration tasks."
}
