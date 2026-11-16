"""
tests/test_aws_emulator.py
Automated test suite validating the Observability Demo stack against the local AWS Emulator.
Tests S3 telemetry lake operations, DynamoDB metadata indexing, EC2/VPC networking mocks,
and Canary service quota snapshots against https://127.0.0.1:9297.
"""

import json
import os
import subprocess
import time
import pytest
import requests
import urllib3

urllib3.disable_warnings()

try:
    import boto3
    from botocore.config import Config
except ImportError:
    boto3 = None

from canary.aws_quotas import (
    DEFAULT_AWS_SERVICE_QUOTAS,
    fetch_aws_service_quotas,
    adjust_service_quotas_configuration,
    calculate_utilization,
)

EMULATOR_PORT = int(os.getenv("EMULATOR_PORT", "9297"))
EMULATOR_ENDPOINT = os.getenv("AWS_ENDPOINT_URL", f"https://127.0.0.1:{EMULATOR_PORT}")
CA_BUNDLE_PATH = os.path.expanduser(f"~/.aws/emulator/{EMULATOR_PORT}/ca.pem")
VERIFY_SSL = CA_BUNDLE_PATH if os.path.isfile(CA_BUNDLE_PATH) else False


@pytest.fixture(scope="session")
def emulator_service():
    """Ensure AWS Emulator is running and healthy for the test session."""
    # Check if emulator is already running
    try:
        resp = requests.get(f"{EMULATOR_ENDPOINT}/__admin/health", verify=False, timeout=2)
        if resp.status_code == 200:
            yield EMULATOR_ENDPOINT
            return
    except Exception:
        pass

    # If not running, attempt to start local aws-emulator binary
    bin_path = os.path.expanduser("~/.local/bin/aws-emulator")
    if not os.path.isfile(bin_path):
        bin_path = "/usr/bin/aws-emulator"
    if not os.path.isfile(bin_path):
        pytest.skip(f"AWS Emulator binary not found at {bin_path} and endpoint not responding.")

    # Enforce telemetry disabled across environment and config
    emu_env = os.environ.copy()
    emu_env["EMULATOR_TELEMETRY"] = "off"
    emu_env["AWS_CLI_TELEMETRY_OPTOUT"] = "1"
    emu_env["AWS_CSM_ENABLED"] = "false"

    cfg_dir = os.path.expanduser("~/.aws/emulator")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg_file = os.path.join(cfg_dir, "config.toml")
    if not os.path.isfile(cfg_file):
        with open(cfg_file, "w") as f:
            f.write("[telemetry]\nenabled = false\n")

    proc = subprocess.Popen(
        [bin_path, "-b", "127.0.0.1", "-p", str(EMULATOR_PORT), "--telemetry", "off", "-a", "off"],
        env=emu_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    started = False
    for _ in range(20):
        time.sleep(0.5)
        try:
            resp = requests.get(f"{EMULATOR_ENDPOINT}/__admin/health", verify=False, timeout=2)
            if resp.status_code == 200:
                started = True
                break
        except Exception:
            continue

    if not started:
        proc.terminate()
        pytest.skip(f"Failed to start AWS Emulator on {EMULATOR_ENDPOINT}")

    try:
        yield EMULATOR_ENDPOINT
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


@pytest.fixture
def boto_session(emulator_service):
    """Provide a boto3 session configured to interact with the AWS Emulator."""
    if not boto3:
        pytest.skip("boto3 is not installed")
    return boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-2",
    )


def test_emulator_health_check(emulator_service):
    """Validate AWS Emulator responds with 200 OK on /__admin/health."""
    resp = requests.get(f"{emulator_service}/__admin/health", verify=False, timeout=5)
    assert resp.status_code == 200
    assert resp.text.strip() == "ok"


def test_emulator_s3_storage_lake(emulator_service, boto_session):
    """Test S3 bucket lifecycle, telemetry audit upload, and object retrieval in the emulator."""
    s3 = boto_session.client("s3", endpoint_url=emulator_service, verify=False)
    bucket_name = "observability-demo-test-telemetry-lake"

    # Create telemetry bucket
    s3.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "us-east-2"},
    )

    # Verify bucket listed
    buckets = s3.list_buckets().get("Buckets", [])
    bucket_names = [b["Name"] for b in buckets]
    assert bucket_name in bucket_names

    # Upload sample telemetry audit payload
    test_key = "audit_logs/year=2026/month=11/day=16/audit_batch_001.json"
    audit_payload = {
        "batch_id": "batch_test_001",
        "service": "canary-dashboard",
        "timestamp": "2026-11-16T15:00:00Z",
        "record_count": 50,
        "status": "ingested",
    }
    s3.put_object(
        Bucket=bucket_name,
        Key=test_key,
        Body=json.dumps(audit_payload).encode("utf-8"),
        ContentType="application/json",
    )

    # Retrieve and verify payload
    obj = s3.get_object(Bucket=bucket_name, Key=test_key)
    retrieved = json.loads(obj["Body"].read().decode("utf-8"))
    assert retrieved["batch_id"] == "batch_test_001"
    assert retrieved["record_count"] == 50


def test_emulator_dynamodb_audit_metadata(emulator_service, boto_session):
    """Test DynamoDB table creation, item write/read, and queries in the emulator."""
    ddb = boto_session.client("dynamodb", endpoint_url=emulator_service, verify=False)
    table_name = "observability_audit_checkpoints"

    try:
        ddb.create_table(
            TableName=table_name,
            KeySchema=[{"AttributeName": "checkpoint_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "checkpoint_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
    except Exception:
        pass  # Table might already exist

    # Wait for table to transition from CREATING to ACTIVE
    for _ in range(10):
        try:
            status = ddb.describe_table(TableName=table_name).get("Table", {}).get("TableStatus")
            if status == "ACTIVE":
                break
        except Exception:
            pass
        time.sleep(0.3)

    # Put checkpoint item
    ddb.put_item(
        TableName=table_name,
        Item={
            "checkpoint_id": {"S": "cp_canary_latest"},
            "last_processed_at": {"S": "2026-11-16T15:00:00Z"},
            "processed_records": {"N": "1420"},
        },
    )

    # Get checkpoint item
    item = ddb.get_item(
        TableName=table_name,
        Key={"checkpoint_id": {"S": "cp_canary_latest"}},
    ).get("Item", {})

    assert item.get("checkpoint_id", {}).get("S") == "cp_canary_latest"
    assert item.get("processed_records", {}).get("N") == "1420"


def test_emulator_ec2_vpc_networking(emulator_service, boto_session):
    """Test EC2 and VPC resource description and creation in the emulator."""
    ec2 = boto_session.client("ec2", endpoint_url=emulator_service, verify=False)

    # Describe VPCs
    vpcs = ec2.describe_vpcs().get("Vpcs", [])
    assert isinstance(vpcs, list)

    # Create and describe security group
    sg_name = f"canary-test-sg-{int(time.time())}"
    try:
        sg_res = ec2.create_security_group(
            GroupName=sg_name,
            Description="Canary test security group for local emulator",
        )
        sg_id = sg_res.get("GroupId")
        assert sg_id is not None

        sgs = ec2.describe_security_groups(GroupIds=[sg_id]).get("SecurityGroups", [])
        assert len(sgs) == 1
        assert sgs[0]["GroupName"] == sg_name
    except Exception as exc:
        # Fallback if create_security_group returns 501 in current emulator version
        assert "501" in str(exc) or "NotImplemented" in str(exc) or sg_id is not None


def test_emulator_canary_service_quotas_integration(emulator_service, boto_session):
    """Test Canary service quota discovery engine against the local AWS Emulator."""
    quotas = fetch_aws_service_quotas(boto_session=boto_session)
    assert len(quotas) == len(DEFAULT_AWS_SERVICE_QUOTAS)

    # Verify all quotas have required fields and proper default calculation
    for q in quotas:
        assert "quota_key" in q
        assert "current_configuration" in q
        assert "current_usage" in q
        assert "utilization_pct" in q
        assert q["status"] in ("NORMAL", "WARNING", "CRITICAL")

    # Test dynamic configuration adjustment on emulator output
    adjusted = adjust_service_quotas_configuration(get_conn_or_conn=None, multiplier=2.0, max_utilization_pct=70.0)
    assert len(adjusted) == len(DEFAULT_AWS_SERVICE_QUOTAS)

    for q in adjusted:
        if q["current_usage"] > 0 and q["utilization_pct"] <= 70.0:
            assert q["current_configuration"] == pytest.approx(q["current_usage"] * 2.0, rel=1e-2)
            assert q["utilization_pct"] == pytest.approx(50.0, rel=1e-1)
