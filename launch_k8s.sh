#!/usr/bin/env bash
# launch_k8s.sh — manage the observability stack on a local Kind Kubernetes cluster.
#
# Handles dependency installation (docker, kubectl, kind), cluster creation,
# image builds, Kind image loading, ConfigMap provisioning, and manifest apply.
#
# Run with no arguments (or --help) to see this usage message.

set -euo pipefail

cd "$(dirname "$0")"

CLUSTER_NAME="observability-cluster"
RUN=false
TEARDOWN_ONLY=false

for arg in "$@"; do
    case "$arg" in
        --run)           RUN=true ;;
        --teardown-only) TEARDOWN_ONLY=true ;;
        --help|-h)       ;;  # handled below
    esac
done

# ---------------------------------------------------------------------------
# Help — shown when no meaningful action is requested
# ---------------------------------------------------------------------------
if [ "$RUN" = false ] && [ "$TEARDOWN_ONLY" = false ]; then
    cat <<'EOF'
Usage: launch_k8s.sh [OPTIONS]

Options:
  --run              Install dependencies, create Kind cluster (if needed),
                     build and load images, apply manifests, wait for pods
  --teardown-only    Delete the Kind cluster and all its resources, then exit
  --help, -h         Show this help message and exit

Examples:
  ./launch_k8s.sh --run              # full setup and deploy
  ./launch_k8s.sh --teardown-only    # destroy the cluster cleanly

Services (after --run):
  Python App   http://localhost:5000
  Java App     http://localhost:8080
  Rust App     http://localhost:8081
  Grafana      http://localhost:3000
  Tempo        http://localhost:3200
  Loki         http://localhost:3100
  Mimir        http://localhost:9009
EOF
    exit 0
fi

# ---------------------------------------------------------------------------
# Teardown — delete the Kind cluster (removes all pods, volumes, networks)
# ---------------------------------------------------------------------------
teardown() {
    echo "=== Tearing down Kind cluster: $CLUSTER_NAME ==="
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        kind delete cluster --name "$CLUSTER_NAME"
        echo "  Cluster '$CLUSTER_NAME' deleted."
    else
        echo "  No cluster named '$CLUSTER_NAME' found — nothing to do."
    fi
}

if [ "$TEARDOWN_ONLY" = true ]; then
    teardown
    echo ""
    echo "=== Teardown complete (--teardown-only; skipping build and launch) ==="
    exit 0
fi

# ---------------------------------------------------------------------------
# --run: full setup path from here
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Step 1: Dependency checks — install docker, kubectl, kind if missing
# ---------------------------------------------------------------------------
echo "=== Checking dependencies ==="

if ! docker ps >/dev/null 2>&1; then
    echo "  Docker not found or not accessible. Installing Docker..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    sudo systemctl start docker
    sudo systemctl enable docker
    echo "  ✓ Docker installed"
else
    echo "  ✓ Docker"
fi

if ! command -v kubectl >/dev/null 2>&1; then
    echo "  kubectl not found. Installing kubectl..."
    curl -sLO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
    rm kubectl
    echo "  ✓ kubectl installed"
else
    echo "  ✓ kubectl"
fi

if ! command -v kind >/dev/null 2>&1; then
    echo "  kind not found. Installing kind..."
    curl -sLo ./kind https://kind.sigs.k8s.io/dl/v0.22.0/kind-linux-amd64
    sudo install -o root -g root -m 0755 kind /usr/local/bin/kind
    rm kind
    echo "  ✓ kind installed"
else
    echo "  ✓ kind"
fi

# ---------------------------------------------------------------------------
# Step 2: Create Kind cluster (skip if already exists)
# ---------------------------------------------------------------------------
echo ""
echo "=== Setting up Kind cluster: $CLUSTER_NAME ==="

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "  Cluster '$CLUSTER_NAME' already exists — reusing."
    kubectl config use-context "kind-${CLUSTER_NAME}"
else
    echo "  Creating cluster '$CLUSTER_NAME'..."
    kind create cluster --name "$CLUSTER_NAME" --config - <<'KINDCONF'
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
    echo "  ✓ Cluster created"
fi

# ---------------------------------------------------------------------------
# Step 3: Build application images
# ---------------------------------------------------------------------------
echo ""
echo "=== Building Docker images ==="
docker build -t observability-python-app:latest .
docker build -t observability-canary:latest ./canary
docker build -t observability-java-app:latest ./java-app
docker build -t observability-rust-app:latest ./rust-app
echo "  ✓ All images built"

# ---------------------------------------------------------------------------
# Step 4: Load images into Kind (they don't have registry access by default)
# ---------------------------------------------------------------------------
echo ""
echo "=== Loading images into Kind cluster ==="
kind load docker-image observability-python-app:latest --name "$CLUSTER_NAME"
kind load docker-image observability-canary:latest      --name "$CLUSTER_NAME"
kind load docker-image observability-java-app:latest    --name "$CLUSTER_NAME"
kind load docker-image observability-rust-app:latest    --name "$CLUSTER_NAME"
echo "  ✓ Images loaded"

# ---------------------------------------------------------------------------
# Step 5: Apply ConfigMaps from local config files
# ---------------------------------------------------------------------------
echo ""
echo "=== Applying ConfigMaps ==="
kubectl create configmap otel-config \
    --from-file=config.yaml=./config/otel-collector.yaml \
    -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap tempo-config \
    --from-file=config.yaml=./config/tempo.yaml \
    -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap loki-config \
    --from-file=config.yaml=./config/loki.yaml \
    -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap mimir-config \
    --from-file=config.yaml=./config/mimir.yaml \
    -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap prometheus-config \
    --from-file=prometheus.yml=./config/prometheus.yaml \
    -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap grafana-provisioning \
    --from-file=datasources.yaml=./config/grafana-datasources.yaml \
    --from-file=dashboard.yml=./config/grafana-dashboard-provider.yaml \
    -o yaml --dry-run=client | kubectl apply -f -
kubectl create configmap grafana-dashboards \
    --from-file=./config/dashboards/ \
    -o yaml --dry-run=client | kubectl apply -f -
echo "  ✓ ConfigMaps applied"

# ---------------------------------------------------------------------------
# Step 6: Apply Kubernetes manifests
# ---------------------------------------------------------------------------
echo ""
echo "=== Applying manifests ==="
kubectl apply -f k8s/manifests/
echo "  ✓ Manifests applied"

# ---------------------------------------------------------------------------
# Step 7: Wait for all pods to be ready
# ---------------------------------------------------------------------------
echo ""
echo "=== Waiting for pods to be ready ==="

wait_for_deployment() {
    local name="$1" timeout="${2:-180}"
    echo -n "  Waiting for $name"
    if kubectl rollout status deployment/"$name" --timeout="${timeout}s" >/dev/null 2>&1; then
        echo " ✓"
    else
        echo " ✗ (timed out after ${timeout}s)"
    fi
}

wait_for_deployment tempo               180
wait_for_deployment loki                120
wait_for_deployment mimir               120
wait_for_deployment prometheus          60
wait_for_deployment otel-collector      60
wait_for_deployment grafana             120
wait_for_deployment postgres            60
wait_for_deployment observability-python-app 120
wait_for_deployment observability-java-app   120
wait_for_deployment observability-rust-app   120

# ---------------------------------------------------------------------------
# Step 8: Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Pod status ==="
kubectl get pods -o wide

echo ""
echo "  Python App   http://localhost:5000"
echo "  Java App     http://localhost:8080"
echo "  Rust App     http://localhost:8081"
echo "  Grafana      http://localhost:3000"
echo "  Tempo        http://localhost:3200"
echo "  Loki         http://localhost:3100"
echo "  Mimir        http://localhost:9009"
echo ""
echo "  Logs:  kubectl logs -f deployment/observability-python-app"
echo "  Pods:  kubectl get pods"
