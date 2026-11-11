# ------------------------------------------------------------------ IAM for the chatbot (IRSA)
# Only the "chatbot" Kubernetes service account can assume this role, and the role can only invoke
# Bedrock models: least privilege.
data "aws_iam_policy_document" "chatbot_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:helpdesk:chatbot"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "chatbot" {
  name               = "${local.name}-chatbot"
  assume_role_policy = data.aws_iam_policy_document.chatbot_assume.json
}

resource "aws_iam_role_policy" "chatbot_bedrock" {
  name = "invoke-bedrock"
  role = aws_iam_role.chatbot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      Resource = "*"
    }]
  })
}

# Notifications pod: may publish only to the alerts topic (and use the KMS key that encrypts it)
data "aws_iam_policy_document" "notifications_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:helpdesk:notifications"]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "notifications" {
  name               = "${local.name}-notifications"
  assume_role_policy = data.aws_iam_policy_document.notifications_assume.json
}

resource "aws_iam_role_policy" "notifications_sns" {
  name = "publish-alerts"
  role = aws_iam_role.notifications.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.main.arn },
    ]
  })
}

# ------------------------------------------------------------------ cost guard-rail
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
