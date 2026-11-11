variable "project" {
  description = "Name prefix for every resource"
  type        = string
  default     = "helpdesk-platform"
}

variable "region" {
  description = "AWS region (pick one where Claude is available on Amazon Bedrock)"
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.30.0.0/16"
}

variable "kubernetes_version" {
  type    = string
  default = "1.31"
}

variable "node_instance_type" {
  description = "EKS worker size. t3.medium = 2 vCPU / 4 GB"
  type        = string
  default     = "t3.medium"
}

variable "node_min" {
  type    = number
  default = 1
}

variable "node_desired" {
  type    = number
  default = 2
}

variable "node_max" {
  description = "Upper limit on worker nodes = upper limit on EC2 cost"
  type        = number
  default     = 3
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "budget_limit_usd" {
  description = "Monthly AWS Budget; you get an email at 80% and 100%"
  type        = number
  default     = 20
}

variable "budget_email" {
  description = "Email address for budget alerts"
  type        = string
}

variable "db_multi_az" {
  description = "Standby database in a second AZ (automatic failover). Doubles the RDS cost."
  type        = bool
  default     = false
}

variable "waf_rate_limit" {
  description = "Max requests per 5 minutes from one IP before AWS WAF blocks it"
  type        = number
  default     = 1000
}
