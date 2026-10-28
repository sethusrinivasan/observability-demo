"""
tests/test_terraform_iac.py — Validation tests for Terraform IaC and deploy_aws.sh

Guarantees that:
1. All AWS resources are tagged with ApplicationName = "Observability-Demo".
2. Terraform syntax and formatting are 100% valid.
3. deploy_aws.sh is executable and responds to commands.
4. Lambda configurations enforce Graviton (arm64) and frugal 128MB MicroVM sizing.
"""

import os
import stat
import subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TF_DIR = (REPO_ROOT / "deployment" / "terraform") if (REPO_ROOT / "deployment" / "terraform").exists() else (REPO_ROOT / "terraform")
ROOT_DEPLOY_SCRIPT = REPO_ROOT / "deploy_aws.sh"
DEPLOY_SCRIPT = (REPO_ROOT / "deployment" / "deploy_aws.sh") if (REPO_ROOT / "deployment" / "deploy_aws.sh").exists() else ROOT_DEPLOY_SCRIPT


def test_deploy_script_exists_and_executable():
    """Verify deploy_aws.sh exists and has execute permissions."""
    assert ROOT_DEPLOY_SCRIPT.exists(), "deploy_aws.sh does not exist at repo root"
    assert DEPLOY_SCRIPT.exists(), "deployment/deploy_aws.sh does not exist"
    mode = os.stat(ROOT_DEPLOY_SCRIPT).st_mode
    assert bool(mode & stat.S_IXUSR), "deploy_aws.sh is not executable"
    mode_deploy = os.stat(DEPLOY_SCRIPT).st_mode
    assert bool(mode_deploy & stat.S_IXUSR), "deployment/deploy_aws.sh is not executable"


def test_deploy_script_help_command():
    """Verify deploy_aws.sh help command works cleanly."""
    proc = subprocess.run([str(DEPLOY_SCRIPT), "help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "ApplicationName" in proc.stdout
    assert "Observability-Demo" in proc.stdout
    assert "init" in proc.stdout
    assert "apply" in proc.stdout
    assert "deploy" in proc.stdout


def test_terraform_files_exist():
    """Verify all expected Terraform modular files exist."""
    required_files = [
        "main.tf",
        "variables.tf",
        "s3.tf",
        "ecr.tf",
        "iam.tf",
        "lambda.tf",
        "eventbridge.tf",
        "outputs.tf",
        "versions.tf"
    ]
    for tf_file in required_files:
        path = TF_DIR / tf_file
        assert path.exists(), f"Missing required Terraform file: {tf_file}"


def test_mandatory_application_name_tag():
    """Verify ApplicationName = 'Observability-Demo' is enforced in Terraform & deploy script."""
    main_tf = (TF_DIR / "main.tf").read_text()
    assert 'ApplicationName = "Observability-Demo"' in main_tf, (
        "Mandatory default_tag ApplicationName = 'Observability-Demo' missing in main.tf"
    )

    outputs_tf = (TF_DIR / "outputs.tf").read_text()
    assert 'value       = "Observability-Demo"' in outputs_tf, (
        "ApplicationName tag output missing in outputs.tf"
    )

    script_content = DEPLOY_SCRIPT.read_text()
    assert 'APP_TAG="Observability-Demo"' in script_content, (
        "APP_TAG in deploy_aws.sh must be 'Observability-Demo'"
    )


def test_lambda_frugal_microvm_configurations():
    """Verify Lambda functions enforce arm64 Graviton and 128MB capacity configurations."""
    lambda_tf = (TF_DIR / "lambda.tf").read_text()
    assert 'architectures = ["arm64"]' in lambda_tf, "Lambda functions must use Graviton (arm64)"
    assert 'memory_size   = 128' in lambda_tf or 'memory_size = 128' in lambda_tf
    assert 'reserved_concurrent_executions = var.collector_reserved_concurrency' in lambda_tf
    assert "MAX_PARQUET_FILE_SIZE_BYTES" in lambda_tf
    assert "10485760" in lambda_tf


def test_terraform_fmt_and_validate():
    """Verify Terraform configuration passes fmt check and validation."""
    fmt_proc = subprocess.run(["terraform", "fmt", "-check", str(TF_DIR)], capture_output=True, text=True)
    assert fmt_proc.returncode == 0, f"Terraform formatting errors:\n{fmt_proc.stdout}\n{fmt_proc.stderr}"

    val_proc = subprocess.run(
        ["terraform", f"-chdir={TF_DIR}", "validate"],
        capture_output=True,
        text=True
    )
    assert val_proc.returncode == 0, f"Terraform validation failed:\n{val_proc.stderr}"


def test_deploy_workflow_actions_configuration():
    """Verify deploy-aws-terraform.yml supports Destroy and redeploy manually while push runs apply."""
    import yaml
    workflow_path = REPO_ROOT / ".github" / "workflows" / "deploy-aws-terraform.yml"
    assert workflow_path.exists(), "deploy-aws-terraform.yml does not exist"

    raw = workflow_path.read_text()
    data = yaml.safe_load(raw)

    # In YAML 1.1, on: is parsed as True
    on_triggers = data.get("on") or data.get(True)
    assert on_triggers is not None, "Workflow triggers missing"

    # Push on main must be configured for incremental updates
    assert "push" in on_triggers
    assert "main" in on_triggers["push"]["branches"]

    # workflow_dispatch must include 'Destroy and redeploy' option
    assert "workflow_dispatch" in on_triggers
    inputs = on_triggers["workflow_dispatch"]["inputs"]
    assert "action" in inputs
    options = inputs["action"]["options"]
    assert "Destroy and redeploy" in options
    assert "apply" in options
    assert "plan" in options
    assert "destroy" in options

    # Inspect job steps
    steps = data["jobs"]["terraform"]["steps"]
    step_map = {s.get("id", s.get("name")): s for s in steps}

    # Destroy step must exist and be guarded to manual triggers only
    destroy_step = step_map.get("destroy")
    assert destroy_step is not None, "Destroy step missing"
    assert "Destroy and redeploy" in destroy_step["if"]
    assert "github.event_name == 'push'" not in destroy_step["if"], "Destroy step must never run on push"

    # S3 numbered suffix archive step must exist prior to destroy
    archive_step = step_map.get("archive_s3")
    assert archive_step is not None, "Archive S3 step missing"
    assert "Destroy and redeploy" in archive_step["if"]
    assert "github.event_name == 'push'" not in archive_step["if"], "Archive step must never run on push"

    # Verify archive step comes before destroy step in execution sequence
    step_ids = [s.get("id") for s in steps]
    assert step_ids.index("archive_s3") < step_ids.index("destroy"), "S3 archive step must run before destroy step"

    # Apply step must run on push and on Destroy and redeploy
    apply_step = step_map.get("apply")
    assert apply_step is not None, "Apply step missing"
    assert "github.event_name == 'push'" in apply_step["if"]
    assert "Destroy and redeploy" in apply_step["if"]
