#!/usr/bin/env bash
# k8s/deploy-k8s.sh - Setup Kind cluster and deploy the observability stack

set -euo pipefail

CLUSTER_NAME="observability-cluster"

echo "=== Checking Dependencies ==="
if ! command -v docker &> /dev/null; then
    echo "Docker is required but not installed. Please install Docker first."
    exit 1
fi

if ! command -v kubectl &> /dev/null; then
    echo "kubectl not found. Installing kubectl..."
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
    rm kubectl
fi

if ! command -v kind &> /dev/null; then
    echo "kind not found. Installing kind..."
    [ $(uname -m) = x86_64 ] && curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.22.0/kind-linux-amd64
    sudo install -o root -g root -m 0755 kind /usr/local/bin/kind
    rm kind
fi

echo "=== Checking for Kind cluster ==="
if ! kind get clusters 2>/dev/null | grep -q "$CLUSTER_NAME"; then
    echo "Creating new Kind cluster: $CLUSTER_NAME"
    kind create cluster --name "$CLUSTER_NAME" --config - <<KINDCONF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 30000
    hostPort: 3000
    protocol: TCP
  - containerPort: 30001
    hostPort: 5000
    protocol: TCP
  - containerPort: 30002
    hostPort: 3100
    protocol: TCP
  - containerPort: 30003
    hostPort: 3200
    protocol: TCP
  - containerPort: 30004
    hostPort: 9009
    protocol: TCP
KINDCONF
else
    echo "Cluster $CLUSTER_NAME already exists."
    kubectl config use-context kind-$CLUSTER_NAME
fi

echo "=== Building Docker Images ==="
docker build -t observability-python-app:latest .
docker build -t observability-canary:latest ./canary

echo "=== Loading Images into Kind ==="
kind load docker-image observability-python-app:latest --name "$CLUSTER_NAME"
kind load docker-image observability-canary:latest --name "$CLUSTER_NAME"

echo "=== Creating ConfigMaps ==="
kubectl create configmap otel-config --from-file=config.yaml=./config/otel-collector.yaml -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap tempo-config --from-file=config.yaml=./config/tempo.yaml -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap loki-config --from-file=config.yaml=./config/loki.yaml -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap mimir-config --from-file=config.yaml=./config/mimir.yaml -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap prometheus-config --from-file=prometheus.yml=./config/prometheus.yaml -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap grafana-provisioning \
  --from-file=datasources.yaml=./config/grafana-datasources.yaml \
  --from-file=dashboard.yml=./config/grafana-dashboard-provider.yaml \
  -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap grafana-dashboards --from-file=./config/dashboards/ -o yaml --dry-run=client | kubectl apply -f -

echo "=== Applying Kubernetes Manifests ==="
kubectl apply -f k8s/manifests/

echo "=== deployment complete ==="
