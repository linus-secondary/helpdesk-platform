#!/usr/bin/env bash
# Deploys (or updates) the helpdesk on the EKS cluster created by Terraform.
#   bash scripts/aws-deploy.sh                 # build + push image, then deploy
#   SKIP_BUILD=1 IMAGE_TAG=abc123 bash ...     # deploy an image already in ECR (used by Jenkins)
# Run "terraform apply" (terraform/) and the CloudFormation stack first: see README section 11.
set -euo pipefail
cd "$(dirname "$0")/.."

APP=helpdesk-platform
NS=helpdesk
REGION=${AWS_REGION:-us-east-1}
IMAGE_TAG=${IMAGE_TAG:-latest}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
IMAGE=$REGISTRY/$APP

echo "==> 1/7 kubectl access to EKS"
aws eks update-kubeconfig --region "$REGION" --name "$APP" >/dev/null

if [ -z "${SKIP_BUILD:-}" ]; then
  echo "==> 2/7 build and push image $IMAGE:$IMAGE_TAG"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"
  docker build --platform linux/amd64 -t "$IMAGE:$IMAGE_TAG" app
  docker push "$IMAGE:$IMAGE_TAG"
else
  echo "==> 2/7 using existing image $IMAGE:$IMAGE_TAG"
fi

echo "==> 3/7 metrics-server (needed by the autoscalers)"
kubectl get deployment metrics-server -n kube-system >/dev/null 2>&1 \
  || kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml

echo "==> 4/7 look up AWS resources created by Terraform"
WAF_ACL_ARN=$(aws wafv2 list-web-acls --scope REGIONAL --region "$REGION" \
                --query "WebACLs[?Name=='$APP-waf'].ARN | [0]" --output text)
SNS_TOPIC_ARN=$(aws sns list-topics --region "$REGION" --query "Topics[?ends_with(TopicArn, ':$APP-alerts')].TopicArn | [0]" --output text)
CHATBOT_ROLE_ARN=$(aws iam get-role --role-name "$APP-chatbot" --query Role.Arn --output text)
NOTIF_ROLE_ARN=$(aws iam get-role --role-name "$APP-notifications" --query Role.Arn --output text)
DATABASE_URL=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$APP/database-url" \
                 --query SecretString --output text)
for v in WAF_ACL_ARN SNS_TOPIC_ARN; do
  [ "${!v}" != "None" ] && [ -n "${!v}" ] || { echo "ERROR: $v not found - run terraform apply first"; exit 1; }
done

BUILD=$(mktemp -d)
cp -r k8s "$BUILD/"
OVERLAY=$BUILD/k8s/overlays/aws

echo "==> 5/7 create the Application Load Balancer (Ingress + WAF) and wait for its hostname"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
sed "s|WAF_ACL_ARN_PLACEHOLDER|$WAF_ACL_ARN|" "$OVERLAY/ingress.yaml" | kubectl apply -n "$NS" -f - >/dev/null
for i in $(seq 1 60); do
  ALB_HOST=$(kubectl get ingress helpdesk -n "$NS" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
  [ -n "$ALB_HOST" ] && break
  sleep 5
done
[ -n "$ALB_HOST" ] || { echo "ERROR: ALB has no hostname. Check: kubectl describe ingress helpdesk -n $NS"; exit 1; }
PUBLIC_URL="http://$ALB_HOST"

echo "==> 6/7 secrets and environment-specific values"
existing() { kubectl get secret app-secrets -n "$NS" -o "jsonpath={.data.$1}" 2>/dev/null | base64 -d 2>/dev/null || true; }
INTERNAL_TOKEN=$(existing INTERNAL_TOKEN); INTERNAL_TOKEN=${INTERNAL_TOKEN:-$(openssl rand -hex 24)}
KC_PASS=$(existing KEYCLOAK_ADMIN_PASSWORD); KC_PASS=${KC_PASS:-$(openssl rand -hex 12)}
cat > "$OVERLAY/secrets.env" <<EOF
DATABASE_URL=$DATABASE_URL
INTERNAL_TOKEN=$INTERNAL_TOKEN
KEYCLOAK_ADMIN_USER=admin
KEYCLOAK_ADMIN_PASSWORD=$KC_PASS
EOF
sed -i.bak "s|PUBLIC_URL_PLACEHOLDER|$PUBLIC_URL|g; s|SNS_TOPIC_ARN_PLACEHOLDER|$SNS_TOPIC_ARN|; s|AWS_REGION=.*|AWS_REGION=$REGION|" "$OVERLAY/config.env"
sed -i.bak "s|PUBLIC_URL_PLACEHOLDER|$PUBLIC_URL|g" "$OVERLAY/patch-keycloak.yaml"
sed -i.bak "s|WAF_ACL_ARN_PLACEHOLDER|$WAF_ACL_ARN|" "$OVERLAY/ingress.yaml"
sed -i.bak "s|CHATBOT_ROLE_ARN_PLACEHOLDER|$CHATBOT_ROLE_ARN|; s|NOTIFICATIONS_ROLE_ARN_PLACEHOLDER|$NOTIF_ROLE_ARN|" "$OVERLAY/patch-irsa.yaml"
sed -i.bak "s|newName: .*|newName: $IMAGE|; s|newTag: .*|newTag: $IMAGE_TAG|" "$OVERLAY/kustomization.yaml"
REALM=$BUILD/k8s/base/keycloak/helpdesk-realm.json
sed -i.bak "s|\"http://localhost:8000/\*\"\]|\"http://localhost:8000/*\", \"$PUBLIC_URL/*\"]|; s|\"webOrigins\": \[\"http://localhost:8000\"\]|\"webOrigins\": [\"http://localhost:8000\", \"$PUBLIC_URL\"]|; s|\"post.logout.redirect.uris\": \"http://localhost:8000/\*\"|\"post.logout.redirect.uris\": \"http://localhost:8000/*##$PUBLIC_URL/*\"|" "$REALM"

echo "==> 7/7 apply manifests"
kubectl apply -k "$OVERLAY"
for d in redis keycloak gateway tickets sla notifications chatbot; do
  kubectl rollout status "deployment/$d" -n "$NS" --timeout=600s
done
rm -rf "$BUILD"

echo
echo "  Web app : $PUBLIC_URL            (allow 2-3 minutes for the ALB to pass health checks)"
echo "  Keycloak: $PUBLIC_URL/auth/admin  (user: admin, password: $KC_PASS)"
echo "  Test    : python scripts/smoke_test.py $PUBLIC_URL"
echo
echo "  Note: the realm is imported only on Keycloak's FIRST start. If the ALB hostname changed,"
echo "  run: kubectl delete pod -n $NS -l app=keycloak"
