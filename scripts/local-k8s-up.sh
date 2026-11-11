#!/usr/bin/env bash
# Runs the whole platform on a local Kubernetes cluster (minikube). Free.
#   bash scripts/local-k8s-up.sh
# Then in two terminals:
#   kubectl port-forward -n helpdesk svc/gateway 8000:80
#   kubectl port-forward -n helpdesk svc/keycloak 8080:8080
set -euo pipefail
cd "$(dirname "$0")/.."

minikube status >/dev/null 2>&1 || minikube start --cpus=4 --memory=6g
minikube addons enable metrics-server >/dev/null

echo "==> building the image inside minikube"
minikube image build -t helpdesk-platform:local app

echo "==> applying manifests"
kubectl apply -k k8s/overlays/local
for d in redis keycloak gateway tickets sla notifications chatbot; do
  kubectl rollout status "deployment/$d" -n helpdesk --timeout=600s
done
kubectl get pods -n helpdesk
echo
echo "Now run (each in its own terminal):"
echo "  kubectl port-forward -n helpdesk svc/gateway 8000:80"
echo "  kubectl port-forward -n helpdesk svc/keycloak 8080:8080"
echo "and open http://localhost:8000"
