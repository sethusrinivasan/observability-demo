#!/usr/bin/env bash
# k8s/deploy-k8s.sh - Setup Kind cluster and deploy the observability stack

set -euo pipefail

CLUSTER_NAME="observability-cluster"

echo "=== Checking Dependencies ==="
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing Docker..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    sudo systemctl start docker
    sudo systemctl enable docker
    
    # Add current user to docker group if needed (requires relogin, so we'll use sudo for the check)
    echo "Verifying Docker installation..."
    if ! sudo docker run --rm hello-world; then
        echo "Docker installation verification failed."
        exit 1
    fi
    echo "Docker installed and verified successfully."
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
  - containerPort: 30005
    hostPort: 8080
    protocol: TCP
  - containerPort: 30006
    hostPort: 8081
    protocol: TCP
KINDCONF
else
    echo "Cluster $CLUSTER_NAME already exists."
    kubectl config use-context kind-$CLUSTER_NAME
fi

echo "=== Building Docker Images ==="
docker build -t observability-python-app:latest .
docker build -t observability-canary:latest ./canary
docker build -t observability-java-app:latest ./java-app
docker build -t observability-rust-app:latest ./rust-app

echo "=== Loading Images into Kind ==="
kind load docker-image observability-python-app:latest --name "$CLUSTER_NAME"
kind load docker-image observability-canary:latest --name "$CLUSTER_NAME"
kind load docker-image observability-java-app:latest --name "$CLUSTER_NAME"
kind load docker-image observability-rust-app:latest --name "$CLUSTER_NAME"

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

echo "=== starting automated verification ==="
bash tests/run_all_tests.sh
