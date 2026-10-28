"""
canary/aws_quotas.py
AWS Service Quotas & Utilization Snapshot Engine for Amazon RDS, EC2, VPC, Lambda, S3, CloudWatch, and DynamoDB.

Captures, snapshots, and monitors AWS Service Quotas, applied limits, current usage,
and utilization percentages. Runs automated snapshots twice every day (every 12 hours)
and stores historical snapshot logs and latest quota states in Aurora / PostgreSQL tables:
- aws_service_quotas_snapshots (historical snapshot records)
- aws_service_quotas_latest (latest quota configuration, usage, and utilization%)

Supports live AWS APIs via boto3 with graceful fallback for local and testing environments.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("canary.aws_quotas")

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Standard Master AWS Service Quotas Catalog
DEFAULT_AWS_SERVICE_QUOTAS = [
    # ---------------- Amazon RDS / Aurora ----------------
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-7B6409FD",
        "quota_name": "DB instances",
        "default_quota": 40.0,
        "current_configuration": 40.0,
        "current_usage": 2.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-F6136E67",
        "quota_name": "DB clusters",
        "default_quota": 40.0,
        "current_configuration": 40.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-03BB86AE",
        "quota_name": "Total storage for all DB instances (TB)",
        "default_quota": 100.0,
        "current_configuration": 100.0,
        "current_usage": 20.0,
        "unit": "TB",
        "is_adjustable": True,
    },
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-DEAB8280",
        "quota_name": "Manual DB instance snapshots",
        "default_quota": 100.0,
        "current_configuration": 100.0,
        "current_usage": 3.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-272F2071",
        "quota_name": "Subnet groups",
        "default_quota": 50.0,
        "current_configuration": 50.0,
        "current_usage": 2.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-977F928C",
        "quota_name": "Parameter groups",
        "default_quota": 50.0,
        "current_configuration": 50.0,
        "current_usage": 3.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-C5320F88",
        "quota_name": "Read replicas per master",
        "default_quota": 15.0,
        "current_configuration": 15.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "rds",
        "service_name": "Amazon Relational Database Service (RDS)",
        "quota_code": "L-DF7C56F3",
        "quota_name": "Event subscriptions",
        "default_quota": 50.0,
        "current_configuration": 50.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },

    # ---------------- Amazon EC2 ----------------
    {
        "service_code": "ec2",
        "service_name": "Amazon Elastic Compute Cloud (EC2)",
        "quota_code": "L-1216C47A",
        "quota_name": "Running On-Demand Standard (A, C, D, I, M, R, T, Z) instances (vCPU)",
        "default_quota": 32.0,
        "current_configuration": 32.0,
        "current_usage": 8.0,
        "unit": "vCPU",
        "is_adjustable": True,
    },
    {
        "service_code": "ec2",
        "service_name": "Amazon Elastic Compute Cloud (EC2)",
        "quota_code": "L-34B43A08",
        "quota_name": "Elastic IP addresses (EIPs)",
        "default_quota": 5.0,
        "current_configuration": 5.0,
        "current_usage": 2.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "ec2",
        "service_name": "Amazon Elastic Compute Cloud (EC2)",
        "quota_code": "L-E8E98BEF",
        "quota_name": "Security groups per VPC",
        "default_quota": 2500.0,
        "current_configuration": 2500.0,
        "current_usage": 12.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "ec2",
        "service_name": "Amazon Elastic Compute Cloud (EC2)",
        "quota_code": "L-0263D0A3",
        "quota_name": "Inbound or outbound rules per security group",
        "default_quota": 60.0,
        "current_configuration": 60.0,
        "current_usage": 14.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "ec2",
        "service_name": "Amazon Elastic Compute Cloud (EC2)",
        "quota_code": "L-30DA4279",
        "quota_name": "Key pairs per region",
        "default_quota": 5000.0,
        "current_configuration": 5000.0,
        "current_usage": 3.0,
        "unit": "Count",
        "is_adjustable": True,
    },

    # ---------------- Amazon VPC ----------------
    {
        "service_code": "vpc",
        "service_name": "Amazon Virtual Private Cloud (VPC)",
        "quota_code": "L-F678F142",
        "quota_name": "VPCs per Region",
        "default_quota": 5.0,
        "current_configuration": 5.0,
        "current_usage": 2.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "vpc",
        "service_name": "Amazon Virtual Private Cloud (VPC)",
        "quota_code": "L-40773211",
        "quota_name": "Subnets per VPC",
        "default_quota": 200.0,
        "current_configuration": 200.0,
        "current_usage": 6.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "vpc",
        "service_name": "Amazon Virtual Private Cloud (VPC)",
        "quota_code": "L-A4707E72",
        "quota_name": "Internet gateways per Region",
        "default_quota": 5.0,
        "current_configuration": 5.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "vpc",
        "service_name": "Amazon Virtual Private Cloud (VPC)",
        "quota_code": "L-FE5A380F",
        "quota_name": "NAT gateways per Availability Zone",
        "default_quota": 5.0,
        "current_configuration": 5.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "vpc",
        "service_name": "Amazon Virtual Private Cloud (VPC)",
        "quota_code": "L-589F43AA",
        "quota_name": "Route tables per VPC",
        "default_quota": 200.0,
        "current_configuration": 200.0,
        "current_usage": 4.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "vpc",
        "service_name": "Amazon Virtual Private Cloud (VPC)",
        "quota_code": "L-29B6465B",
        "quota_name": "Network ACLs per VPC",
        "default_quota": 200.0,
        "current_configuration": 200.0,
        "current_usage": 2.0,
        "unit": "Count",
        "is_adjustable": True,
    },

    # ---------------- AWS Lambda ----------------
    {
        "service_code": "lambda",
        "service_name": "AWS Lambda",
        "quota_code": "L-B99A9384",
        "quota_name": "Concurrent executions",
        "default_quota": 1000.0,
        "current_configuration": 1000.0,
        "current_usage": 15.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "lambda",
        "service_name": "AWS Lambda",
        "quota_code": "L-87E7D513",
        "quota_name": "Function and layer storage (GB)",
        "default_quota": 75.0,
        "current_configuration": 75.0,
        "current_usage": 8.5,
        "unit": "GB",
        "is_adjustable": True,
    },
    {
        "service_code": "lambda",
        "service_name": "AWS Lambda",
        "quota_code": "L-CD2B2222",
        "quota_name": "Function memory allocation (MB)",
        "default_quota": 10240.0,
        "current_configuration": 10240.0,
        "current_usage": 1024.0,
        "unit": "MB",
        "is_adjustable": False,
    },
    {
        "service_code": "lambda",
        "service_name": "AWS Lambda",
        "quota_code": "L-490333D7",
        "quota_name": "Functions per Region",
        "default_quota": 10000.0,
        "current_configuration": 10000.0,
        "current_usage": 4.0,
        "unit": "Count",
        "is_adjustable": True,
    },

    # ---------------- Amazon S3 ----------------
    {
        "service_code": "s3",
        "service_name": "Amazon Simple Storage Service (S3)",
        "quota_code": "L-DC2B2D3D",
        "quota_name": "General purpose buckets per account",
        "default_quota": 100.0,
        "current_configuration": 100.0,
        "current_usage": 4.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "s3",
        "service_name": "Amazon Simple Storage Service (S3)",
        "quota_code": "L-E10E5A8E",
        "quota_name": "Access Points per account and Region",
        "default_quota": 1000.0,
        "current_configuration": 1000.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "s3",
        "service_name": "Amazon Simple Storage Service (S3)",
        "quota_code": "L-5E8BD65B",
        "quota_name": "Multi-Region Access Points per account",
        "default_quota": 5.0,
        "current_configuration": 5.0,
        "current_usage": 0.0,
        "unit": "Count",
        "is_adjustable": True,
    },

    # ---------------- Amazon CloudWatch ----------------
    {
        "service_code": "cloudwatch",
        "service_name": "Amazon CloudWatch",
        "quota_code": "L-89753DE8",
        "quota_name": "Metric alarms per Region",
        "default_quota": 5000.0,
        "current_configuration": 5000.0,
        "current_usage": 18.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "cloudwatch",
        "service_name": "Amazon CloudWatch",
        "quota_code": "L-0010996F",
        "quota_name": "Composite alarms per Region",
        "default_quota": 500.0,
        "current_configuration": 500.0,
        "current_usage": 2.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "cloudwatch",
        "service_name": "Amazon CloudWatch",
        "quota_code": "L-6DA6D81C",
        "quota_name": "Dashboards per account",
        "default_quota": 500.0,
        "current_configuration": 500.0,
        "current_usage": 4.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "cloudwatch",
        "service_name": "Amazon CloudWatch",
        "quota_code": "L-29BF9D41",
        "quota_name": "Metric streams per Region",
        "default_quota": 20.0,
        "current_configuration": 20.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },

    # ---------------- Amazon DynamoDB ----------------
    {
        "service_code": "dynamodb",
        "service_name": "Amazon DynamoDB",
        "quota_code": "L-F98FE922",
        "quota_name": "Tables per Region",
        "default_quota": 2500.0,
        "current_configuration": 2500.0,
        "current_usage": 3.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "dynamodb",
        "service_name": "Amazon DynamoDB",
        "quota_code": "L-B76295D3",
        "quota_name": "Global secondary indexes per table",
        "default_quota": 20.0,
        "current_configuration": 20.0,
        "current_usage": 1.0,
        "unit": "Count",
        "is_adjustable": True,
    },
    {
        "service_code": "dynamodb",
        "service_name": "Amazon DynamoDB",
        "quota_code": "L-7D267812",
        "quota_name": "Read capacity units (RCU) per table",
        "default_quota": 40000.0,
        "current_configuration": 40000.0,
        "current_usage": 1500.0,
        "unit": "Units",
        "is_adjustable": True,
    },
    {
        "service_code": "dynamodb",
        "service_name": "Amazon DynamoDB",
        "quota_code": "L-D318218C",
        "quota_name": "Write capacity units (WCU) per table",
        "default_quota": 40000.0,
        "current_configuration": 40000.0,
        "current_usage": 1500.0,
        "unit": "Units",
        "is_adjustable": True,
    },
]

# In-memory stores
LATEST_QUOTAS_CACHE: list[dict] = []
SNAPSHOTS_HISTORY_CACHE: list[dict] = []
_QUOTAS_LOCK = threading.Lock()

# Scheduler state (Twice Daily = every 12 hours = 43200 seconds)
SCHEDULER_INTERVAL_SECONDS = 43200  # 12 hours (Twice Daily)
SCHEDULER_STATE = {
    "enabled": True,
    "interval_seconds": SCHEDULER_INTERVAL_SECONDS,
    "interval_hours": 12,
    "frequency": "Twice Daily (Every 12h)",
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_snapshot_id": None,
    "last_error": None,
    "total_monitored": len(DEFAULT_AWS_SERVICE_QUOTAS),
}
_SCHEDULER_THREAD = None
_SCHEDULER_TRIGGER_EVENT = threading.Event()
_SCHEDULER_STOP_EVENT = threading.Event()


def calculate_utilization(current_usage: float, current_configuration: float) -> tuple[float, str]:
    """Calculate utilization percentage and risk status."""
    limit = max(0.0001, float(current_configuration))
    usage = max(0.0, float(current_usage))
    pct = round((usage / limit) * 100.0, 1)

    if pct >= 85.0:
        status = "CRITICAL"
    elif pct >= 70.0:
        status = "WARNING"
    else:
        status = "NORMAL"

    return pct, status


def init_service_quotas_tables(conn) -> bool:
    """Initialize the service quotas tables in the Aurora PostgreSQL database."""
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            # 1. Historical snapshots log table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS aws_service_quotas_snapshots (
                    snapshot_id VARCHAR(64) PRIMARY KEY,
                    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    total_quotas INT NOT NULL DEFAULT 0,
                    warning_quotas INT NOT NULL DEFAULT 0,
                    critical_quotas INT NOT NULL DEFAULT 0,
                    avg_utilization_pct NUMERIC(6, 2) NOT NULL DEFAULT 0.0,
                    service_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
                    raw_quotas_json JSONB NOT NULL DEFAULT '[]'::jsonb
                );
                CREATE INDEX IF NOT EXISTS idx_aws_quotas_snapshots_captured_at
                    ON aws_service_quotas_snapshots (captured_at DESC);
            """)

            # 2. Latest quota state table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS aws_service_quotas_latest (
                    quota_key VARCHAR(128) PRIMARY KEY,
                    service_code VARCHAR(64) NOT NULL,
                    service_name VARCHAR(128) NOT NULL,
                    quota_code VARCHAR(64) NOT NULL,
                    quota_name VARCHAR(256) NOT NULL,
                    default_quota NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
                    current_configuration NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
                    current_usage NUMERIC(14, 2) NOT NULL DEFAULT 0.0,
                    utilization_pct NUMERIC(6, 2) NOT NULL DEFAULT 0.0,
                    unit VARCHAR(32) NOT NULL DEFAULT '',
                    is_adjustable BOOLEAN NOT NULL DEFAULT true,
                    status VARCHAR(32) NOT NULL DEFAULT 'NORMAL',
                    last_snapshot_id VARCHAR(64),
                    last_snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_aws_quotas_latest_service
                    ON aws_service_quotas_latest (service_code);
                CREATE INDEX IF NOT EXISTS idx_aws_quotas_latest_status
                    ON aws_service_quotas_latest (status);
            """)
            conn.commit()
            logger.info("Initialized Aurora / PostgreSQL AWS service quotas tables successfully.")
            return True
    except Exception as exc:
        logger.warning("Error initializing AWS service quotas tables: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _try_fetch_live_aws_quotas(boto_session=None) -> dict[str, dict]:
    """Attempt to fetch live applied quota limits and real resource counts from AWS."""
    if not boto3:
        return {}

    live_data: dict[str, dict] = {}
    region = AWS_REGION

    try:
        # Check if AWS credentials or IAM role are active
        session = boto_session or boto3.Session(region_name=region)
        sq_client = session.client("service-quotas", region_name=region)

        # 1. RDS applied quotas & usage
        try:
            rds_client = session.client("rds", region_name=region)
            dbs = rds_client.describe_db_instances().get("DBInstances", [])
            clusters = rds_client.describe_db_clusters().get("DBClusters", [])
            snaps = rds_client.describe_db_snapshots().get("DBSnapshots", [])
            total_storage = sum(db.get("AllocatedStorage", 0) for db in dbs) / 1024.0  # GB to TB

            live_data["rds:L-7B6409FD"] = {"usage": float(len(dbs))}
            live_data["rds:L-F6136E67"] = {"usage": float(len(clusters))}
            live_data["rds:L-03BB86AE"] = {"usage": round(max(1.0, total_storage), 2)}
            live_data["rds:L-DEAB8280"] = {"usage": float(len(snaps))}
        except (BotoCoreError, ClientError, Exception) as rds_err:
            logger.debug("RDS live quota probe skipped: %s", rds_err)

        # 2. EC2 applied quotas & usage
        try:
            ec2_client = session.client("ec2", region_name=region)
            sgs = ec2_client.describe_security_groups().get("SecurityGroups", [])
            eips = ec2_client.describe_addresses().get("Addresses", [])
            keys = ec2_client.describe_key_pairs().get("KeyPairs", [])
            live_data["ec2:L-E8E98BEF"] = {"usage": float(len(sgs))}
            live_data["ec2:L-34B43A08"] = {"usage": float(len(eips))}
            live_data["ec2:L-30DA4279"] = {"usage": float(len(keys))}
        except (BotoCoreError, ClientError, Exception) as ec2_err:
            logger.debug("EC2 live quota probe skipped: %s", ec2_err)

        # 3. VPC applied quotas & usage
        try:
            ec2_client = session.client("ec2", region_name=region)
            vpcs = ec2_client.describe_vpcs().get("Vpcs", [])
            subnets = ec2_client.describe_subnets().get("Subnets", [])
            igws = ec2_client.describe_internet_gateways().get("InternetGateways", [])
            nat_gws = ec2_client.describe_nat_gateways().get("NatGateways", [])
            live_data["vpc:L-F678F142"] = {"usage": float(len(vpcs))}
            live_data["vpc:L-40773211"] = {"usage": float(len(subnets))}
            live_data["vpc:L-A4707E72"] = {"usage": float(len(igws))}
            live_data["vpc:L-FE5A380F"] = {"usage": float(len(nat_gws))}
        except (BotoCoreError, ClientError, Exception) as vpc_err:
            logger.debug("VPC live quota probe skipped: %s", vpc_err)

        # 4. Lambda applied quotas & usage
        try:
            lam_client = session.client("lambda", region_name=region)
            account_settings = lam_client.get_account_settings()
            usage = account_settings.get("AccountUsage", {})
            limits = account_settings.get("AccountLimit", {})
            func_count = usage.get("FunctionCount", 0)
            code_storage_gb = usage.get("TotalCodeSize", 0) / (1024.0 ** 3)
            concurrency = limits.get("ConcurrentExecutions", 1000)

            live_data["lambda:L-490333D7"] = {"usage": float(func_count)}
            live_data["lambda:L-87E7D513"] = {"usage": round(max(0.5, code_storage_gb), 2)}
            live_data["lambda:L-B99A9384"] = {"config": float(concurrency)}
        except (BotoCoreError, ClientError, Exception) as lam_err:
            logger.debug("Lambda live quota probe skipped: %s", lam_err)

        # 5. S3 applied quotas & usage
        try:
            s3_client = session.client("s3", region_name=region)
            buckets = s3_client.list_buckets().get("Buckets", [])
            live_data["s3:L-DC2B2D3D"] = {"usage": float(len(buckets))}
        except (BotoCoreError, ClientError, Exception) as s3_err:
            logger.debug("S3 live quota probe skipped: %s", s3_err)

        # 6. Service Quotas Client applied configuration overrides
        try:
            for service in ["rds", "ec2", "vpc", "lambda", "s3", "cloudwatch", "dynamodb"]:
                try:
                    paginator = sq_client.get_paginator("list_service_quotas")
                    for page in paginator.paginate(ServiceCode=service):
                        for q in page.get("Quotas", []):
                            key = f"{service}:{q.get('QuotaCode')}"
                            val = q.get("Value")
                            if val is not None:
                                live_data.setdefault(key, {})["config"] = float(val)
                except Exception:
                    continue
        except Exception:
            pass

    except Exception as general_err:
        logger.debug("Live AWS Service Quotas discovery general exception: %s", general_err)

    return live_data


def fetch_aws_service_quotas(boto_session=None) -> list[dict]:
    """
    Fetch current AWS service quotas, applied configurations, current usage, and utilization %.
    Uses live AWS APIs when credentials exist, with robust deterministic fallback.
    """
    live_overrides = _try_fetch_live_aws_quotas(boto_session)
    now_iso = datetime.now(timezone.utc).isoformat()

    quotas_result: list[dict] = []
    for base in DEFAULT_AWS_SERVICE_QUOTAS:
        service_code = base["service_code"]
        quota_code = base["quota_code"]
        quota_key = f"{service_code}:{quota_code}"

        # Default quota
        default_quota = float(base.get("default_quota", 0.0))

        # Current configuration (applied limit)
        current_config = float(base.get("current_configuration", default_quota))
        if quota_key in live_overrides and "config" in live_overrides[quota_key]:
            current_config = float(live_overrides[quota_key]["config"])

        # Current usage
        current_usage = float(base.get("current_usage", 0.0))
        if quota_key in live_overrides and "usage" in live_overrides[quota_key]:
            current_usage = float(live_overrides[quota_key]["usage"])

        utilization_pct, status = calculate_utilization(current_usage, current_config)

        item = {
            "quota_key": quota_key,
            "service_code": service_code,
            "service_name": base["service_name"],
            "quota_code": quota_code,
            "quota_name": base["quota_name"],
            "default_quota": default_quota,
            "current_configuration": current_config,
            "current_usage": current_usage,
            "utilization_pct": utilization_pct,
            "unit": base.get("unit", "Count"),
            "is_adjustable": base.get("is_adjustable", True),
            "status": status,
            "captured_at": now_iso,
        }
        quotas_result.append(item)

    return quotas_result


def capture_service_quotas_snapshot(get_conn_or_conn=None, boto_session=None) -> dict:
    """
    Execute a snapshot of AWS Service Quotas, calculate metrics,
    persist the snapshot and latest records in PostgreSQL / Aurora,
    and update in-memory caches and scheduler state.
    """
    now = datetime.now(timezone.utc)
    snapshot_id = f"snap_{now.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    now_iso = now.isoformat()

    # 1. Fetch latest quotas
    quotas = fetch_aws_service_quotas(boto_session=boto_session)

    # 2. Compute aggregate metrics
    total_quotas = len(quotas)
    warning_quotas = sum(1 for q in quotas if q["status"] == "WARNING")
    critical_quotas = sum(1 for q in quotas if q["status"] == "CRITICAL")
    avg_utilization = (
        round(sum(q["utilization_pct"] for q in quotas) / max(1, total_quotas), 2)
        if total_quotas > 0
        else 0.0
    )

    service_counts: dict[str, int] = {}
    for q in quotas:
        sc = q["service_code"]
        service_counts[sc] = service_counts.get(sc, 0) + 1

    snapshot_record = {
        "snapshot_id": snapshot_id,
        "captured_at": now_iso,
        "total_quotas": total_quotas,
        "warning_quotas": warning_quotas,
        "critical_quotas": critical_quotas,
        "avg_utilization_pct": avg_utilization,
        "service_counts": service_counts,
        "quotas": quotas,
    }

    # 3. Update in-memory cache
    with _QUOTAS_LOCK:
        global LATEST_QUOTAS_CACHE, SNAPSHOTS_HISTORY_CACHE
        for q in quotas:
            q["last_snapshot_id"] = snapshot_id
            q["last_snapshot_at"] = now_iso
        LATEST_QUOTAS_CACHE = list(quotas)
        SNAPSHOTS_HISTORY_CACHE.insert(0, {
            "snapshot_id": snapshot_id,
            "captured_at": now_iso,
            "total_quotas": total_quotas,
            "warning_quotas": warning_quotas,
            "critical_quotas": critical_quotas,
            "avg_utilization_pct": avg_utilization,
            "service_counts": service_counts,
        })
        # Keep recent 100 snapshots in cache
        if len(SNAPSHOTS_HISTORY_CACHE) > 100:
            SNAPSHOTS_HISTORY_CACHE = SNAPSHOTS_HISTORY_CACHE[:100]

    # 4. Persist to PostgreSQL / Aurora if connection available
    conn = None
    should_close = False
    if callable(get_conn_or_conn):
        try:
            conn = get_conn_or_conn()
            should_close = True
        except Exception as conn_err:
            logger.debug("Failed acquiring DB connection for quota snapshot: %s", conn_err)
    elif get_conn_or_conn:
        conn = get_conn_or_conn

    if conn:
        try:
            init_service_quotas_tables(conn)
            with conn.cursor() as cur:
                # Insert snapshot record
                cur.execute("""
                    INSERT INTO aws_service_quotas_snapshots
                    (snapshot_id, captured_at, total_quotas, warning_quotas, critical_quotas, avg_utilization_pct, service_counts, raw_quotas_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (snapshot_id) DO NOTHING;
                """, (
                    snapshot_id,
                    now,
                    total_quotas,
                    warning_quotas,
                    critical_quotas,
                    avg_utilization,
                    json.dumps(service_counts),
                    json.dumps(quotas),
                ))

                # Upsert into latest table
                for q in quotas:
                    cur.execute("""
                        INSERT INTO aws_service_quotas_latest
                        (quota_key, service_code, service_name, quota_code, quota_name, default_quota, current_configuration, current_usage, utilization_pct, unit, is_adjustable, status, last_snapshot_id, last_snapshot_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (quota_key) DO UPDATE SET
                            service_name = EXCLUDED.service_name,
                            quota_name = EXCLUDED.quota_name,
                            default_quota = EXCLUDED.default_quota,
                            current_configuration = EXCLUDED.current_configuration,
                            current_usage = EXCLUDED.current_usage,
                            utilization_pct = EXCLUDED.utilization_pct,
                            unit = EXCLUDED.unit,
                            is_adjustable = EXCLUDED.is_adjustable,
                            status = EXCLUDED.status,
                            last_snapshot_id = EXCLUDED.last_snapshot_id,
                            last_snapshot_at = EXCLUDED.last_snapshot_at;
                    """, (
                        q["quota_key"],
                        q["service_code"],
                        q["service_name"],
                        q["quota_code"],
                        q["quota_name"],
                        q["default_quota"],
                        q["current_configuration"],
                        q["current_usage"],
                        q["utilization_pct"],
                        q["unit"],
                        q["is_adjustable"],
                        q["status"],
                        snapshot_id,
                        now,
                    ))

                conn.commit()
                logger.info("Persisted AWS service quotas snapshot %s (%d quotas) to Aurora PostgreSQL.", snapshot_id, total_quotas)
        except Exception as db_err:
            logger.warning("Error persisting AWS service quotas snapshot: %s", db_err)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            if should_close:
                try:
                    conn.close()
                except Exception:
                    pass

    # 5. Update scheduler state
    SCHEDULER_STATE["last_run_at"] = now_iso
    SCHEDULER_STATE["run_count"] += 1
    SCHEDULER_STATE["last_snapshot_id"] = snapshot_id
    SCHEDULER_STATE["total_monitored"] = total_quotas
    next_dt = datetime.fromtimestamp(now.timestamp() + SCHEDULER_STATE["interval_seconds"], timezone.utc)
    SCHEDULER_STATE["next_run_at"] = next_dt.isoformat()

    return snapshot_record


def get_latest_service_quotas(get_conn_or_conn=None) -> list[dict]:
    """Retrieve the latest service quotas state from PostgreSQL or memory cache."""
    global LATEST_QUOTAS_CACHE
    conn = None
    should_close = False
    if callable(get_conn_or_conn):
        try:
            conn = get_conn_or_conn(read_only=True)
            should_close = True
        except Exception:
            try:
                conn = get_conn_or_conn()
                should_close = True
            except Exception:
                conn = None
    elif get_conn_or_conn:
        conn = get_conn_or_conn

    if conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor if psycopg2 else None) as cur:
                cur.execute("""
                    SELECT quota_key, service_code, service_name, quota_code, quota_name,
                           default_quota, current_configuration, current_usage, utilization_pct,
                           unit, is_adjustable, status, last_snapshot_id, last_snapshot_at
                    FROM aws_service_quotas_latest
                    ORDER BY service_code ASC, utilization_pct DESC;
                """)
                rows = cur.fetchall()
                if rows:
                    results = []
                    for r in rows:
                        results.append({
                            "quota_key": r["quota_key"],
                            "service_code": r["service_code"],
                            "service_name": r["service_name"],
                            "quota_code": r["quota_code"],
                            "quota_name": r["quota_name"],
                            "default_quota": float(r["default_quota"]),
                            "current_configuration": float(r["current_configuration"]),
                            "current_usage": float(r["current_usage"]),
                            "utilization_pct": float(r["utilization_pct"]),
                            "unit": r["unit"],
                            "is_adjustable": bool(r["is_adjustable"]),
                            "status": r["status"],
                            "last_snapshot_id": r["last_snapshot_id"],
                            "last_snapshot_at": r["last_snapshot_at"].isoformat() if hasattr(r["last_snapshot_at"], "isoformat") else str(r["last_snapshot_at"]),
                        })
                    return results
        except Exception as exc:
            logger.debug("Database read for aws_service_quotas_latest failed, using cache: %s", exc)
        finally:
            if should_close:
                try:
                    conn.close()
                except Exception:
                    pass

    # Fallback to in-memory cache or freshly generated default quotas
    with _QUOTAS_LOCK:
        if LATEST_QUOTAS_CACHE:
            return list(LATEST_QUOTAS_CACHE)

    # Initialize cache if empty
    fresh = fetch_aws_service_quotas()
    with _QUOTAS_LOCK:
        LATEST_QUOTAS_CACHE = list(fresh)
    return fresh


def get_recent_snapshots_log(get_conn_or_conn=None, limit: int = 20) -> list[dict]:
    """Retrieve the recent snapshots log from PostgreSQL or memory cache."""
    conn = None
    should_close = False
    if callable(get_conn_or_conn):
        try:
            conn = get_conn_or_conn(read_only=True)
            should_close = True
        except Exception:
            try:
                conn = get_conn_or_conn()
                should_close = True
            except Exception:
                conn = None
    elif get_conn_or_conn:
        conn = get_conn_or_conn

    if conn:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor if psycopg2 else None) as cur:
                cur.execute("""
                    SELECT snapshot_id, captured_at, total_quotas, warning_quotas, critical_quotas,
                           avg_utilization_pct, service_counts
                    FROM aws_service_quotas_snapshots
                    ORDER BY captured_at DESC
                    LIMIT %s;
                """, (limit,))
                rows = cur.fetchall()
                if rows:
                    results = []
                    for r in rows:
                        sc = r["service_counts"]
                        if isinstance(sc, str):
                            try:
                                sc = json.loads(sc)
                            except Exception:
                                sc = {}
                        results.append({
                            "snapshot_id": r["snapshot_id"],
                            "captured_at": r["captured_at"].isoformat() if hasattr(r["captured_at"], "isoformat") else str(r["captured_at"]),
                            "total_quotas": int(r["total_quotas"]),
                            "warning_quotas": int(r["warning_quotas"]),
                            "critical_quotas": int(r["critical_quotas"]),
                            "avg_utilization_pct": float(r["avg_utilization_pct"]),
                            "service_counts": sc,
                        })
                    return results
        except Exception as exc:
            logger.debug("Database read for aws_service_quotas_snapshots failed, using cache: %s", exc)
        finally:
            if should_close:
                try:
                    conn.close()
                except Exception:
                    pass

    with _QUOTAS_LOCK:
        return list(SNAPSHOTS_HISTORY_CACHE[:limit])


def get_aws_quotas_scheduler_status() -> dict:
    """Return the status of the twice-daily AWS service quotas snapshot scheduler."""
    return dict(SCHEDULER_STATE)


def trigger_aws_quotas_snapshot_now(get_conn_callable=None) -> dict:
    """Trigger an immediate on-demand snapshot of AWS Service Quotas."""
    snap = capture_service_quotas_snapshot(get_conn_callable)
    _SCHEDULER_TRIGGER_EVENT.set()
    return snap


def _scheduler_loop(get_conn_callable, interval_seconds: int):
    """Background daemon worker for twice-daily (every 12 hours) snapshots."""
    logger.info("AWS Service Quotas Twice-Daily Scheduler started (interval=%ds / 12h).", interval_seconds)

    # Initial snapshot on boot so data is present immediately
    try:
        capture_service_quotas_snapshot(get_conn_callable)
    except Exception as exc:
        logger.warning("Initial AWS service quota snapshot failed: %s", exc)

    while not _SCHEDULER_STOP_EVENT.is_set():
        # Sleep in small slices or wait for trigger event
        triggered = _SCHEDULER_TRIGGER_EVENT.wait(timeout=interval_seconds)
        if _SCHEDULER_STOP_EVENT.is_set():
            break

        if triggered:
            _SCHEDULER_TRIGGER_EVENT.clear()
            # Already executed by trigger function, wait for next cycle
            continue

        try:
            capture_service_quotas_snapshot(get_conn_callable)
        except Exception as exc:
            logger.error("Error during scheduled AWS service quotas snapshot: %s", exc)
            SCHEDULER_STATE["last_error"] = str(exc)


def start_aws_quotas_scheduler(get_conn_callable, interval_seconds: int = SCHEDULER_INTERVAL_SECONDS) -> threading.Thread:
    """Start the background scheduler thread for twice-daily snapshots."""
    global _SCHEDULER_THREAD
    if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
        return _SCHEDULER_THREAD

    SCHEDULER_STATE["interval_seconds"] = interval_seconds
    SCHEDULER_STATE["interval_hours"] = round(interval_seconds / 3600.0, 1)
    SCHEDULER_STATE["enabled"] = True
    _SCHEDULER_STOP_EVENT.clear()
    _SCHEDULER_TRIGGER_EVENT.clear()

    _SCHEDULER_THREAD = threading.Thread(
        target=_scheduler_loop,
        args=(get_conn_callable, interval_seconds),
        daemon=True,
        name="aws-service-quotas-scheduler"
    )
    _SCHEDULER_THREAD.start()
    return _SCHEDULER_THREAD


def stop_aws_quotas_scheduler():
    """Stop the background scheduler thread."""
    _SCHEDULER_STOP_EVENT.set()
    _SCHEDULER_TRIGGER_EVENT.set()
    SCHEDULER_STATE["enabled"] = False
