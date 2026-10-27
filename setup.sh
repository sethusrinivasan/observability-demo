#!/usr/bin/env bash
# ==============================================================================
# setup.sh — Local Machine Setup & Prerequisite Installer
#
# Sets up a fresh local machine with all required tools and dependencies to run,
# test, and showcase the Observability Demo locally.
#
# Usage:
#   ./setup.sh              # Standard local setup (Docker, Python venv, Cloudflare)
#   ./setup.sh --check      # Non-destructive check of installed vs missing tools
#   ./setup.sh --full       # Standard + Kubernetes (kubectl, kind) + Cloud (terraform, aws)
#   ./setup.sh --k8s        # Standard + Kubernetes tools
#   ./setup.sh --cloud      # Standard + Cloud/Terraform tools
#   ./setup.sh --help       # Show this help message
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# ------------------------------------------------------------------------------
# Terminal Colors & Styling
# ------------------------------------------------------------------------------
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
BLUE="\033[0;34m"
RED="\033[0;31m"
CYAN="\033[0;36m"
NC="\033[0m"

log_info()    { echo -e "${BLUE}ℹ${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_err()     { echo -e "${RED}✗${NC} $1"; }
log_header()  { echo -e "\n${BOLD}${CYAN}=== $1 ===${NC}"; }

# ------------------------------------------------------------------------------
# Flags & Arguments
# ------------------------------------------------------------------------------
CHECK_ONLY=false
INSTALL_K8S=false
INSTALL_CLOUD=false

for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=true ;;
        --full)
            INSTALL_K8S=true
            INSTALL_CLOUD=true
            ;;
        --k8s)   INSTALL_K8S=true ;;
        --cloud) INSTALL_CLOUD=true ;;
        --help|-h)
            cat <<'EOF'
Usage: ./setup.sh [OPTIONS]

Quickly prepares a new workstation to build, run, and test the Observability Demo.

Options:
  (no args)    Standard local setup:
               - System essentials (curl, git, jq, certificates)
               - Docker Engine + Docker Compose v2 + Buildx
               - Python 3 virtual environment (.venv) + test/dev dependencies
               - Cloudflare Tunnel client (cloudflared) for live public showcase
               - Default .env initialization

  --check      Diagnostic mode: Inspect current machine without installing anything.
  --full       Standard setup + Kubernetes (kubectl, kind) + Cloud (terraform, aws).
  --k8s        Standard setup + Kubernetes tools (kubectl, kind).
  --cloud      Standard setup + Cloud tools (terraform, aws-cli).
  --help, -h   Display this help message.

Examples:
  ./setup.sh              # Setup for Docker local run & development
  ./setup.sh --check      # Check what is installed on this machine
  ./setup.sh --full       # Complete developer setup including K8s & AWS IaC
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Run ./setup.sh --help for available options."
            exit 1
            ;;
    esac
done

# ------------------------------------------------------------------------------
# System & User Detection
# ------------------------------------------------------------------------------
CURRENT_USER="${SUDO_USER:-$USER}"
ARCH="$(uname -m)"
OS_TYPE="$(uname -s)"

case "$ARCH" in
    x86_64)  ARCH_ALT="amd64" ;;
    aarch64|arm64) ARCH_ALT="arm64" ;;
    *) ARCH_ALT="$ARCH" ;;
esac

run_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        log_err "Root privileges required for: $*"
        log_err "Please run with sudo or install sudo."
        exit 1
    fi
}

# ------------------------------------------------------------------------------
# Diagnostic Check Mode (--check)
# ------------------------------------------------------------------------------
run_check() {
    log_header "Prerequisite Status Check"
    echo "OS: $OS_TYPE | Kernel: $(uname -r) | Arch: $ARCH ($ARCH_ALT)"
    echo "User: $CURRENT_USER | Directory: $SCRIPT_DIR"
    echo ""

    local all_good=true

    check_tool() {
        local name="$1"
        local cmd="$2"
        local req="${3:-Required}"
        if command -v "$cmd" >/dev/null 2>&1; then
            local version
            version="$($cmd --version 2>&1 | head -n 1 || echo "installed")"
            printf "  %-18s ${GREEN}✓ FOUND${NC}    %-12s (%s)\n" "$name" "[$req]" "$version"
        else
            if [ "$req" = "Required" ]; then
                printf "  %-18s ${RED}✗ MISSING${NC}  %-12s\n" "$name" "[$req]"
                all_good=false
            else
                printf "  %-18s ${YELLOW}○ MISSING${NC}  %-12s (Optional)\n" "$name" "[$req]"
            fi
        fi
    }

    check_tool "Docker"         "docker"         "Required"
    check_tool "Docker Compose" "docker"         "Required"
    if command -v docker >/dev/null 2>&1; then
        if docker compose version >/dev/null 2>&1; then
            printf "  %-18s ${GREEN}✓ FOUND${NC}    %-12s (%s)\n" "Compose Plugin" "[Required]" "$(docker compose version 2>&1 | head -n 1)"
        else
            printf "  %-18s ${RED}✗ MISSING${NC}  %-12s (Docker compose plugin not found)\n" "Compose Plugin" "[Required]"
            all_good=false
        fi
    fi

    check_tool "Python 3"       "python3"        "Required"
    check_tool "Pip"            "pip3"           "Required"
    check_tool "Git"            "git"            "Required"
    check_tool "cURL"           "curl"           "Required"
    check_tool "jq"             "jq"             "Required"
    check_tool "cloudflared"    "cloudflared"    "Showcase"
    check_tool "kubectl"        "kubectl"        "K8s"
    check_tool "kind"           "kind"           "K8s"
    check_tool "Terraform"      "terraform"      "Cloud"
    check_tool "AWS CLI"        "aws"            "Cloud"

    echo ""
    if [ -f ".env" ]; then
        printf "  %-18s ${GREEN}✓ FOUND${NC}    .env file configured\n" "Environment"
    else
        printf "  %-18s ${YELLOW}○ MISSING${NC}  .env (run ./setup.sh to generate from .env.example)\n" "Environment"
    fi

    if [ -d ".venv" ]; then
        printf "  %-18s ${GREEN}✓ FOUND${NC}    Python virtualenv active at .venv/\n" "VirtualEnv"
    else
        printf "  %-18s ${YELLOW}○ MISSING${NC}  .venv (run ./setup.sh to create)\n" "VirtualEnv"
    fi

    local git_user=$(git config --global user.name 2>/dev/null || echo "")
    local git_email=$(git config --global user.email 2>/dev/null || echo "")
    if [ -n "$git_user" ] && [ -n "$git_email" ]; then
        printf "  %-18s ${GREEN}✓ CONFIGURED${NC} %s <%s>\n" "Git Identity" "$git_user" "$git_email"
    else
        printf "  %-18s ${YELLOW}○ UNSET${NC}    (git config --global user.name / user.email)\n" "Git Identity"
    fi

    if [ -f "$HOME/.ssh/id_ed25519.pub" ] || [ -f "$HOME/.ssh/id_rsa.pub" ]; then
        printf "  %-18s ${GREEN}✓ READY${NC}    SSH key pair ready for GitHub\n" "Git SSH Key"
    else
        printf "  %-18s ${YELLOW}○ MISSING${NC}  No SSH key pair found in ~/.ssh/\n" "Git SSH Key"
    fi

    echo ""
    if [ "$all_good" = true ]; then
        log_success "All core prerequisites are installed! You are ready to run the stack."
        echo "Run './launch_docker.sh --run' or './start.sh' to launch."
    else
        log_warn "Some required dependencies are missing. Run './setup.sh' to install them."
    fi
}

if [ "$CHECK_ONLY" = true ]; then
    run_check
    exit 0
fi

# ------------------------------------------------------------------------------
# Banner
# ------------------------------------------------------------------------------
echo -e "${BOLD}${CYAN}"
echo "================================================================="
echo "   🛠️  Observability Demo — Local Workstation Setup"
echo "================================================================="
echo -e "${NC}"
echo "Configuring development environment on: $OS_TYPE ($ARCH)"
echo "Target User: $CURRENT_USER"

# ------------------------------------------------------------------------------
# Step 1: System Packages (Debian / Ubuntu)
# ------------------------------------------------------------------------------
if [ -f /etc/debian_version ]; then
    log_header "Step 1: Installing System Packages"
    log_info "Updating apt package index..."
    run_root apt-get update -qq

    log_info "Installing core utilities, python3-venv, and SSL certs..."
    run_root apt-get install -y -qq \
        ca-certificates \
        curl \
        gnupg \
        git \
        jq \
        build-essential \
        libpq-dev \
        python3 \
        python3-pip \
        python3-venv \
        python3-psycopg2 \
        python3-full 2>/dev/null || run_root apt-get install -y -qq \
        ca-certificates curl gnupg git jq build-essential libpq-dev python3 python3-pip python3-venv python3-psycopg2

    log_success "Base system packages installed."
elif [ -f /etc/redhat-release ]; then
    log_header "Step 1: Installing System Packages (RHEL/Fedora)"
    run_root dnf install -y curl git jq python3 python3-pip python3-devel gcc
    log_success "Base system packages installed."
elif [ "$OS_TYPE" = "Darwin" ]; then
    log_header "Step 1: Checking macOS Packages (Homebrew)"
    if ! command -v brew >/dev/null 2>&1; then
        log_err "Homebrew is required on macOS. Please install Homebrew from https://brew.sh/"
        exit 1
    fi
    brew install curl git jq python3 2>/dev/null || true
    log_success "Homebrew packages verified."
fi

# ------------------------------------------------------------------------------
# Step 2: Docker Engine & Compose v2 Installation
# ------------------------------------------------------------------------------
log_header "Step 2: Configuring Docker Engine & Compose"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log_success "Docker Engine and Docker Compose plugin are already installed."
else
    if [ -f /etc/debian_version ]; then
        log_info "Installing official Docker CE & Compose plugin from Docker repository..."
        run_root install -m 0755 -d /etc/apt/keyrings
        run_root curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        run_root chmod a+r /etc/apt/keyrings/docker.asc

        UBUNTU_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-noble}")"
        # Fallback to noble if bleeding-edge distribution repository is not yet mirrored
        if ! curl -fsSL -I "https://download.docker.com/linux/ubuntu/dists/${UBUNTU_CODENAME}/Release" >/dev/null 2>&1; then
            UBUNTU_CODENAME="noble"
        fi

        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME} stable" | \
            run_root tee /etc/apt/sources.list.d/docker.list >/dev/null

        run_root apt-get update -qq
        run_root apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        log_success "Docker CE & Compose plugin installed successfully."
    elif [ "$OS_TYPE" = "Darwin" ]; then
        log_info "On macOS, please ensure Docker Desktop, OrbStack, or Colima is installed and running."
        if command -v colima >/dev/null 2>&1; then
            colima status 2>/dev/null || colima start
        fi
    else
        log_warn "Please install Docker and the docker-compose-plugin manually for your distribution."
    fi
fi

# Start Docker daemon if systemd is active
if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
    log_info "Ensuring Docker daemon is active..."
    run_root systemctl enable --now docker 2>/dev/null || true
fi

# Configure non-root docker group access
if getent group docker >/dev/null 2>&1; then
    if ! id -nG "$CURRENT_USER" | grep -qw "docker"; then
        log_info "Adding user '$CURRENT_USER' to 'docker' group..."
        run_root usermod -aG docker "$CURRENT_USER"
        log_warn "User '$CURRENT_USER' added to 'docker' group. You may need to log out and back in, or run 'newgrp docker'."
    else
        log_success "User '$CURRENT_USER' is already a member of the 'docker' group."
    fi
fi

# ------------------------------------------------------------------------------
# Step 3: Python Virtual Environment (.venv) & Test Dependencies
# ------------------------------------------------------------------------------
log_header "Step 3: Setting Up Python Virtual Environment"

if [ ! -d ".venv" ]; then
    log_info "Creating Python virtual environment in .venv/..."
    python3 -m venv .venv || {
        log_warn "Standard python3 -m venv failed. Attempting with --without-pip..."
        python3 -m venv --without-pip .venv
        curl -sS https://bootstrap.pypa.io/get-pip.py | .venv/bin/python3
    }
fi

log_info "Upgrading pip and installing dev/test dependencies..."
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements-dev.txt -q
if [ -f "canary/requirements.txt" ]; then
    log_info "Installing Canary & DuckDB test requirements..."
    .venv/bin/pip install -r canary/requirements.txt -q
fi
log_success "Python virtual environment configured with pytest, requests, boto3, and pyarrow."

# ------------------------------------------------------------------------------
# Step 4: Cloudflare Tunnel Client (cloudflared)
# ------------------------------------------------------------------------------
log_header "Step 4: Checking Cloudflare Tunnel Client (cloudflared)"

if command -v cloudflared >/dev/null 2>&1; then
    log_success "cloudflared is already installed: $(which cloudflared)"
else
    log_info "Installing cloudflared static binary for public showcase bridge..."
    CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH_ALT}"
    if [ "$OS_TYPE" = "Darwin" ]; then
        CLOUDFLARED_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-${ARCH_ALT}.tgz"
    fi

    TARGET_BIN="/usr/local/bin/cloudflared"
    if [ ! -w "/usr/local/bin" ] && [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            sudo curl -fsSL -o "$TARGET_BIN" "$CLOUDFLARED_URL"
            sudo chmod +x "$TARGET_BIN"
        else
            mkdir -p "${HOME}/.local/bin"
            TARGET_BIN="${HOME}/.local/bin/cloudflared"
            curl -fsSL -o "$TARGET_BIN" "$CLOUDFLARED_URL"
            chmod +x "$TARGET_BIN"
        fi
    else
        run_root curl -fsSL -o "$TARGET_BIN" "$CLOUDFLARED_URL"
        run_root chmod +x "$TARGET_BIN"
    fi
    log_success "cloudflared installed to $TARGET_BIN."
fi

# ------------------------------------------------------------------------------
# Step 5: Kubernetes Tools (Optional / --k8s / --full)
# ------------------------------------------------------------------------------
if [ "$INSTALL_K8S" = true ]; then
    log_header "Step 5: Installing Kubernetes Tools (kubectl, kind)"

    if ! command -v kubectl >/dev/null 2>&1; then
        log_info "Installing kubectl..."
        KUBECTL_URL="https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH_ALT}/kubectl"
        run_root curl -fsSL -o /usr/local/bin/kubectl "$KUBECTL_URL"
        run_root chmod +x /usr/local/bin/kubectl
        log_success "kubectl installed."
    else
        log_success "kubectl already present."
    fi

    if ! command -v kind >/dev/null 2>&1; then
        log_info "Installing kind..."
        KIND_URL="https://kind.sigs.k8s.io/dl/v0.22.0/kind-linux-${ARCH_ALT}"
        run_root curl -fsSL -o /usr/local/bin/kind "$KIND_URL"
        run_root chmod +x /usr/local/bin/kind
        log_success "kind installed."
    else
        log_success "kind already present."
    fi
fi

# ------------------------------------------------------------------------------
# Step 6: Cloud & Terraform Tools (Optional / --cloud / --full)
# ------------------------------------------------------------------------------
if [ "$INSTALL_CLOUD" = true ]; then
    log_header "Step 6: Installing Cloud & Terraform Tools"

    if ! command -v terraform >/dev/null 2>&1; then
        log_info "Installing Terraform..."
        if [ -f /etc/debian_version ]; then
            run_root curl -fsSL https://apt.releases.hashicorp.com/gpg | run_root gpg --dearmor -o /etc/apt/keyrings/hashicorp-archive-keyring.gpg
            echo "deb [signed-by=/etc/apt/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs 2>/dev/null || echo noble) main" | \
                run_root tee /etc/apt/sources.list.d/hashicorp.list >/dev/null
            run_root apt-get update -qq && run_root apt-get install -y -qq terraform
            log_success "Terraform installed."
        fi
    else
        log_success "Terraform already present."
    fi

    if ! command -v aws >/dev/null 2>&1; then
        log_info "Installing AWS CLI v2..."
        TEMP_AWS_DIR="$(mktemp -d)"
        curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o "${TEMP_AWS_DIR}/awscliv2.zip"
        unzip -q -o "${TEMP_AWS_DIR}/awscliv2.zip" -d "${TEMP_AWS_DIR}"
        run_root "${TEMP_AWS_DIR}/aws/install" --update 2>/dev/null || true
        rm -rf "${TEMP_AWS_DIR}"
        log_success "AWS CLI v2 installed."
    else
        log_success "AWS CLI already present."
    fi
fi

# ------------------------------------------------------------------------------
# Step 7: Environment Configuration (.env)
# ------------------------------------------------------------------------------
log_header "Step 7: Environment Configuration"

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        log_info "Creating default .env from .env.example..."
        cp .env.example .env
        log_success ".env created with default local settings."
    fi
else
    log_success ".env already exists."
fi

# ------------------------------------------------------------------------------
# Step 8: Git & GitHub Configuration
# ------------------------------------------------------------------------------
log_header "Step 8: Git & GitHub SSH Configuration"

if [ -z "$(git config --global user.name 2>/dev/null || true)" ]; then
    log_info "Configuring default Git user identity..."
    git config --global user.name "Sethu Srinivasan"
    git config --global user.email "sethusrinivasan@gmail.com"
    git config --global init.defaultBranch main
    git config --global pull.rebase false
    git config --global credential.helper store
    log_success "Git identity configured globally (Sethu Srinivasan <sethusrinivasan@gmail.com>)."
else
    log_success "Git identity already configured: $(git config --global user.name) <$(git config --global user.email)>"
fi

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [ ! -f "$HOME/.ssh/id_ed25519" ] && [ ! -f "$HOME/.ssh/id_rsa" ]; then
    log_info "Generating Ed25519 SSH key for GitHub..."
    ssh-keygen -t ed25519 -C "$(git config --global user.email)" -f "$HOME/.ssh/id_ed25519" -N ""
    log_success "SSH key pair generated at ~/.ssh/id_ed25519"
else
    log_success "SSH key pair already present."
fi

if ! grep -q "github.com" "$HOME/.ssh/known_hosts" 2>/dev/null; then
    log_info "Adding github.com to ~/.ssh/known_hosts..."
    ssh-keyscan -t ed25519,rsa github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
    chmod 644 "$HOME/.ssh/known_hosts"
fi

if [ ! -f "$HOME/.ssh/config" ] || ! grep -q "Host github.com" "$HOME/.ssh/config" 2>/dev/null; then
    cat << 'EOF' >> "$HOME/.ssh/config"
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
EOF
    chmod 600 "$HOME/.ssh/config"
    log_success "Configured ~/.ssh/config for GitHub."
fi

# ------------------------------------------------------------------------------
# Step 9: Final Summary & Launch Guide
# ------------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}=================================================================${NC}"
echo -e "${BOLD}${GREEN}   🎉 Workstation Setup Completed Successfully!${NC}"
echo -e "${BOLD}${GREEN}=================================================================${NC}"
echo ""
echo "Next Steps to Run and Test Locally:"
echo ""
echo "  1. Activate Python virtual environment:"
echo -e "     ${CYAN}source .venv/bin/activate${NC}"
echo ""
echo "  2. Launch the full Docker Compose stack (builds all 9 polyglot apps):"
echo -e "     ${CYAN}./launch_docker.sh --run${NC}   (or ${CYAN}./start.sh${NC})"
echo ""
echo "  3. Open your browser to the central dashboards:"
echo "     - Canary Load & Chaos Dashboard:  http://localhost:8085"
echo "     - Grafana LGTM Observability:     http://localhost:3000 (admin / admin)"
echo ""
echo "  4. Run automated test verification:"
echo -e "     ${CYAN}./test.sh${NC}              # Runs smoke test & unit tests"
echo -e "     ${CYAN}pytest tests/ -v${NC}       # Comprehensive test suite"
echo ""
echo "  5. (Optional) Share a live public showcase with zero port-forwarding:"
echo -e "     ${CYAN}./scripts/showcase_tunnel.sh --quick${NC}"
echo ""
echo "If Docker permissions require a session refresh, run: 'newgrp docker'"
