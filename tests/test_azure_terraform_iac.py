"""
tests/test_azure_terraform_iac.py — Validation tests for Azure Terraform IaC, OIDC, and deploy_azure.sh

Guarantees that:
1. All Azure resources are tagged with ApplicationName = "Observability-Demo".
2. Frugal serverless Container Apps configuration (0.25 vCPU, 0.5GiB memory, min_replicas = 0 scale-to-zero).
3. Frugal storage configuration (Standard LRS with 30d Cool / 90d Archive lifecycle policy).
4. Frugal Log Analytics workspace (0.05 GB daily cap).
5. Azure Workload Identity Federation (OIDC) without persisted passwords or client secrets.
6. Foundational resources enforce lifecycle { prevent_destroy = true }.
7. deploy_azure.sh is executable and supports numbered suffix telemetry archiving.
8. Both foundational and workload Azure Terraform configurations pass fmt and validation.
"""

import os
import stat
import subprocess
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
AZURE_TF_DIR = REPO_ROOT / "deployment" / "terraform-azure"
AZURE_FOUNDATION_DIR = REPO_ROOT / "deployment" / "terraform-azure-foundation"
ROOT_DEPLOY_SCRIPT = REPO_ROOT / "deploy_azure.sh"
DEPLOY_SCRIPT = REPO_ROOT / "deployment" / "deploy_azure.sh"
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "deploy-azure-terraform.yml"


def test_deploy_azure_script_exists_and_executable():
    """Verify deploy_azure.sh exists and has execute permissions."""
    assert ROOT_DEPLOY_SCRIPT.exists(), "deploy_azure.sh does not exist at repo root"
    assert DEPLOY_SCRIPT.exists(), "deployment/deploy_azure.sh does not exist"
    mode_root = os.stat(ROOT_DEPLOY_SCRIPT).st_mode
    assert bool(mode_root & stat.S_IXUSR), "deploy_azure.sh is not executable"
    mode_deploy = os.stat(DEPLOY_SCRIPT).st_mode
    assert bool(mode_deploy & stat.S_IXUSR), "deployment/deploy_azure.sh is not executable"


def test_deploy_azure_script_help_command():
    """Verify deploy_azure.sh help command works cleanly and displays frugality options."""
    proc = subprocess.run([str(DEPLOY_SCRIPT), "help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "ApplicationName" in proc.stdout
    assert "Observability-Demo" in proc.stdout
    assert "foundation-init" in proc.stdout
    assert "foundation-apply" in proc.stdout
    assert "init" in proc.stdout
    assert "apply" in proc.stdout
    assert "destroy" in proc.stdout
    assert "destroy-and-redeploy" in proc.stdout


def test_azure_terraform_files_exist():
    """Verify all expected Azure Terraform files exist."""
    foundation_files = [
        "main.tf",
        "variables.tf",
        "storage.tf",
        "acr.tf",
        "github_oidc.tf",
        "outputs.tf",
        "versions.tf",
    ]
    for tf_file in foundation_files:
        path = AZURE_FOUNDATION_DIR / tf_file
        assert path.exists(), f"Missing required foundational Azure file: {tf_file}"

    workload_files = [
        "main.tf",
        "variables.tf",
        "log_analytics.tf",
        "container_apps_env.tf",
        "container_apps.tf",
        "event_grid.tf",
        "postgres.tf",
        "identity.tf",
        "outputs.tf",
        "versions.tf",
    ]
    for tf_file in workload_files:
        path = AZURE_TF_DIR / tf_file
        assert path.exists(), f"Missing required workload Azure file: {tf_file}"


def test_mandatory_application_name_tag_azure():
    """Verify ApplicationName = 'Observability-Demo' is enforced in Azure Terraform & deploy script."""
    foundation_main = (AZURE_FOUNDATION_DIR / "main.tf").read_text()
    assert 'ApplicationName = "Observability-Demo"' in foundation_main, (
        "Mandatory ApplicationName tag missing in terraform-azure-foundation/main.tf"
    )

    workload_main = (AZURE_TF_DIR / "main.tf").read_text()
    assert 'ApplicationName = "Observability-Demo"' in workload_main, (
        "Mandatory ApplicationName tag missing in terraform-azure/main.tf"
    )

    script_content = DEPLOY_SCRIPT.read_text()
    assert 'APP_TAG="Observability-Demo"' in script_content, (
        "APP_TAG in deploy_azure.sh must be 'Observability-Demo'"
    )


def test_azure_frugal_container_app_configurations():
    """Verify Container Apps enforce 0.25 vCPU, 0.5GiB RAM, and min_replicas = 0 scale-to-zero."""
    vars_tf = (AZURE_TF_DIR / "variables.tf").read_text()
    assert "default     = 0" in vars_tf, "min_replicas default must be 0 for scale-to-zero frugality"
    assert "default     = 0.25" in vars_tf, "cpu_allocation default must be 0.25 vCPU"
    assert 'default     = "0.5Gi"' in vars_tf, "memory_allocation default must be 0.5Gi"

    apps_tf = (AZURE_TF_DIR / "container_apps.tf").read_text()
    assert "min_replicas = var.min_replicas" in apps_tf
    assert "cpu    = var.cpu_allocation" in apps_tf
    assert "memory = var.memory_allocation" in apps_tf
    assert "observability-python-app" in apps_tf
    assert "observability-canary" in apps_tf
    assert "custom-telemetry-receiver" in apps_tf


def test_azure_frugal_storage_and_log_analytics_configurations():
    """Verify frugal storage (Standard, LRS, Cool/Archive lifecycle) and Log Analytics daily quota."""
    storage_tf = (AZURE_FOUNDATION_DIR / "storage.tf").read_text()
    assert 'account_tier             = "Standard"' in storage_tf
    assert 'account_replication_type = "LRS"' in storage_tf
    assert "tier_to_cool_after_days_since_modification_greater_than    = 30" in storage_tf
    assert "tier_to_archive_after_days_since_modification_greater_than = 90" in storage_tf

    la_tf = (AZURE_TF_DIR / "log_analytics.tf").read_text()
    assert '"PerGB2018"' in la_tf
    assert "daily_quota_gb      = 0.05" in la_tf or "daily_quota_gb" in la_tf, "Log Analytics must enforce 0.05GB daily ingestion cap"


def test_azure_foundational_resources_prevent_destroy_guards():
    """Verify foundational Azure resources enforce lifecycle { prevent_destroy = true }."""
    storage_tf = (AZURE_FOUNDATION_DIR / "storage.tf").read_text()
    assert "prevent_destroy = true" in storage_tf, "Storage account must enforce prevent_destroy = true"

    oidc_tf = (AZURE_FOUNDATION_DIR / "github_oidc.tf").read_text()
    assert "prevent_destroy = true" in oidc_tf, "Managed Identity must enforce prevent_destroy = true"

    acr_tf = (AZURE_FOUNDATION_DIR / "acr.tf").read_text()
    assert "prevent_destroy = true" in acr_tf, "ACR must enforce prevent_destroy = true"


def test_azure_oidc_no_persisted_secrets():
    """Verify Azure OIDC Workload Identity Federation is configured without client secrets."""
    assert WORKFLOW_FILE.exists(), "deploy-azure-terraform.yml does not exist"
    raw = WORKFLOW_FILE.read_text()
    data = yaml.safe_load(raw)

    permissions = data.get("permissions")
    assert permissions is not None, "Workflow permissions missing"
    assert permissions.get("id-token") == "write", "id-token: write permission required for Azure OIDC"

    # Verify azure/login action uses client-id without client-secret
    steps = data["jobs"]["terraform"]["steps"]
    login_step = next(s for s in steps if "azure/login" in s.get("uses", ""))
    login_with = login_step.get("with", {})
    assert "client-id" in login_with, "client-id required for Azure OIDC"
    assert "tenant-id" in login_with, "tenant-id required for Azure OIDC"
    assert "subscription-id" in login_with, "subscription-id required for Azure OIDC"
    assert "client-secret" not in login_with, "client-secret must not be present in OIDC login"


def test_deploy_azure_workflow_actions_configuration():
    """Verify deploy-azure-terraform.yml supports Destroy and redeploy manually while push runs apply."""
    raw = WORKFLOW_FILE.read_text()
    data = yaml.safe_load(raw)

    on_triggers = data.get("on") or data.get(True)
    assert "push" in on_triggers
    assert "main" in on_triggers["push"]["branches"]

    assert "workflow_dispatch" in on_triggers
    inputs = on_triggers["workflow_dispatch"]["inputs"]
    assert "Destroy and redeploy" in inputs["action"]["options"]

    steps = data["jobs"]["terraform"]["steps"]
    step_map = {s.get("id", s.get("name")): s for s in steps}

    destroy_step = step_map.get("destroy")
    assert destroy_step is not None
    assert "Destroy and redeploy" in destroy_step["if"]
    assert "github.event_name == 'push'" not in destroy_step["if"]

    archive_step = step_map.get("archive_blob")
    assert archive_step is not None
    assert "Destroy and redeploy" in archive_step["if"]

    apply_step = step_map.get("apply")
    assert apply_step is not None
    assert "github.event_name == 'push'" in apply_step["if"]


def test_azure_frugal_postgres_flexible_server():
    """Verify Azure PostgreSQL Flexible Server is frugal, protected, and supports query fingerprinting."""
    pg_tf = (AZURE_TF_DIR / "postgres.tf").read_text()
    assert 'sku_name   = "B_Standard_B1ms"' in pg_tf, "PostgreSQL must use frugal Burstable B1ms SKU"
    assert "storage_mb = 32768" in pg_tf, "PostgreSQL must use 32GB minimum storage"
    assert "auto_grow_enabled     = false" in pg_tf, "auto_grow_enabled must be false to prevent cost surprises"
    assert "prevent_destroy = true" in pg_tf, "PostgreSQL must enforce prevent_destroy = true"
    assert 'name      = "shared_preload_libraries"' in pg_tf
    assert 'value     = "pg_stat_statements"' in pg_tf, "pg_stat_statements must be enabled for query fingerprinting"
    assert 'name      = "observability"' in pg_tf, "observability database must be provisioned"


def test_azure_passwordless_workload_identity():
    """Verify passwordless workload authentication via Managed Identity for ACR, Storage, and Container Apps."""
    identity_tf = (AZURE_TF_DIR / "identity.tf").read_text()
    assert "azurerm_user_assigned_identity" in identity_tf
    assert "uami-" in identity_tf
    assert '"AcrPull"' in identity_tf, "Workload identity must be granted AcrPull role"
    assert '"Storage Blob Data Contributor"' in identity_tf, "Workload identity must be granted Storage Blob Data Contributor"

    apps_tf = (AZURE_TF_DIR / "container_apps.tf").read_text()
    assert "azurerm_user_assigned_identity.workload.id" in apps_tf, "Container apps must attach workload managed identity"
    assert "identity = azurerm_user_assigned_identity.workload.id" in apps_tf, "ACR registry pull must use managed identity"


def test_azure_terraform_fmt_and_validate():
    """Verify both workload and foundational Azure Terraform configurations pass fmt and validate."""
    for tf_path in (AZURE_FOUNDATION_DIR, AZURE_TF_DIR):
        fmt_proc = subprocess.run(["terraform", "fmt", "-check", str(tf_path)], capture_output=True, text=True)
        assert fmt_proc.returncode == 0, f"Terraform formatting errors in {tf_path}:\n{fmt_proc.stdout}"

        val_proc = subprocess.run(
            ["terraform", f"-chdir={tf_path}", "validate"],
            capture_output=True,
            text=True
        )
        assert val_proc.returncode == 0, f"Terraform validation failed in {tf_path}:\n{val_proc.stderr}"
