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
