variable "aws_region" {
  description = "AWS region used for this deployment and its medical-data residency boundary."
  type        = string
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "production"
}

variable "app_host" {
  description = "Public DNS hostname used by the API and TRUSTED_HOSTS."
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate ARN covering app_host; must be in aws_region."
  type        = string
}

variable "container_image" {
  description = "Immutable container image URI, preferably pinned to a commit SHA."
  type        = string
}

variable "github_repository" {
  description = "GitHub repository in owner/name form, authorized for OIDC-based release deployments."
  type        = string
}

variable "vpc_cidr" {
  description = "Private VPC CIDR."
  type        = string
  default     = "10.40.0.0/16"
}

variable "desired_count" {
  description = "Initial number of API tasks across availability zones."
  type        = number
  default     = 2
}

variable "minimum_count" {
  type    = number
  default = 2
}

variable "maximum_count" {
  type    = number
  default = 20
}
