#!/usr/bin/env bash
# Deletes EVERYTHING on AWS for this project so it stops costing money.
# Order matters: Kubernetes namespace (removes the ALB) -> CloudFormation stack -> Terraform.
set -euo pipefail
cd "$(dirname "$0")/.."
REGION=${AWS_REGION:-us-east-1}
APP=helpdesk-platform

if aws eks describe-cluster --name "$APP" --region "$REGION" >/dev/null 2>&1; then
  aws eks update-kubeconfig --region "$REGION" --name "$APP" >/dev/null
  echo "==> deleting Kubernetes namespace (the AWS Load Balancer Controller removes the ALB)"
  kubectl delete namespace helpdesk --ignore-not-found --wait=true --timeout=10m || true
  sleep 60
fi

if aws cloudformation describe-stacks --stack-name "$APP-security" --region "$REGION" >/dev/null 2>&1; then
  echo "==> emptying the CloudTrail bucket and deleting the CloudFormation stack"
  BUCKET=$(aws cloudformation describe-stacks --stack-name "$APP-security" --region "$REGION" \
             --query "Stacks[0].Outputs[?OutputKey=='TrailBucketName'].OutputValue" --output text)
  if [ -n "$BUCKET" ] && [ "$BUCKET" != "None" ]; then
    aws cloudtrail stop-logging --name "$APP-trail" --region "$REGION" || true
    aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" >/dev/null
  fi
  aws cloudformation delete-stack --stack-name "$APP-security" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$APP-security" --region "$REGION"
fi

echo "==> terraform destroy"
cd terraform
terraform destroy -auto-approve
echo "All AWS resources for $APP are deleted."
