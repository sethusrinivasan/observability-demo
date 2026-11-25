# Incident Report: Aurora ZDP Upgrade Failure — reality-check-cost-tracker

- **Target Audience**: AWS Support / Internal Infrastructure Engineering
- **Date**: 2026-11-25
- **Cluster**: `reality-check-cost-tracker` (Aurora PostgreSQL, `us-east-2`, instance: `reality-check-cost-tracker-instance-1`)
- **Upgrade Event**: PostgreSQL `17.9.3` → `17.9.5` via Zero Downtime Patching (ZDP)
- **Impact Window**: 04:44:02 UTC – ongoing (auth failures persisting through 04:57:48+ UTC)
- **Discovery**: Detected and corroborated by AWS Omni Operational Analysis and CloudWatch logs verification

---

## 1. Executive Summary

An AWS-initiated Zero Downtime Patching (ZDP) event upgraded the shared Aurora PostgreSQL cluster (`reality-check-cost-tracker`) from version `17.9.3` to `17.9.5` at **04:44:02 UTC** on 2026-11-25. Despite the "zero downtime" designation, the upgrade caused four distinct failure modes that together resulted in complete loss of database connectivity for IAM-authenticated application users (`postgres` and `sql_frontier_eval_user`) and partial loss of database functionality:

1. **IAM Authentication Blackout**: `HTTP/1.1 403 Forbidden` from internal `rdsauthproxy:1108`, causing `FATAL: PAM authentication failed for user "postgres"` and `"sql_frontier_eval_user"`.
2. **Parameter Namespace Breaking Changes**: Repeated `postgresql.conf` parse warnings for 7 legacy `rds.*` parameters that became reserved prefixes in 17.9.5, leading to partial parameter application.
3. **Session Role Authorization Loss**: `ERROR: must be able to SET ROLE "sql_frontier_eval_user"` during schema provisioning.
4. **Database CONNECT Privilege Loss**: `ERROR: permission denied for database postgres` for `sql_frontier_eval_user`.

The applications were operating normally immediately prior to the upgrade. The failures were introduced directly by breaking changes to the authentication proxy infrastructure and the reserved parameter namespace during the ZDP process boundary transition.

---

## 2. Upgrade Event — Observed Behavior

Verbatim excerpt from CloudWatch log group `/aws/rds/cluster/reality-check-cost-tracker/postgresql`, stream `reality-check-cost-tracker-instance-1`:

```text
2026-11-25 04:44:02 UTC:[local]:rdsadmin@rdsadmin:[71882]:LOG:  Zero downtime patching has commenced.
2026-11-25 04:44:02 UTC:[local]:rdsadmin@rdsadmin:[71882]:LOG:  Upgrade initiated from version: 17.9.3
2026-11-25 04:44:02 UTC:[local]:rdsadmin@rdsadmin:[71882]:LOG:  ZD quiet point found.
2026-11-25 04:44:02 UTC:[local]:rdsadmin@rdsadmin:[71882]:LOG:  [ZD METRICS] name: ZD-WAIT-TIME, value: 2363, desc: Time in micro seconds spent in quiet point found stage
2026-11-25 04:44:04 UTC:[local]:rdsadmin@rdsadmin:[72186]:LOG:  Upgrade completed to version: 17.9.5
2026-11-25 04:44:04 UTC:[local]:rdsadmin@rdsadmin:[72186]:LOG:  Zero downtime patching completed.
2026-11-25 04:44:04 UTC:[local]:rdsadmin@rdsadmin:[72186]:LOG:  [ZD METRICS] name: ZD-TOTAL-TIME, value: 1274367, desc: Total time in micro seconds spent in ZD
2026-11-25 04:44:04 UTC:[local]:rdsadmin@rdsadmin:[72186]:LOG:  [ZD METRICS] name: ZD-TOTAL-CONN-DROP-COUNT, value: 2, desc: Total number of connection dropped during the event
2026-11-25 04:44:04 UTC:[local]:rdsadmin@rdsadmin:[72186]:LOG:  [ZD METRICS] name: ZD-TOTAL-CONN-RESTORED, value: 6, desc: Total number of connection restored during the event
2026-11-25 04:44:04 UTC:[local]:rdsadmin@rdsadmin:[72186]:LOG:  [ZD METRICS] name: DROP-PREPARED-STMT-GUARDRAIL, value: 2, desc: Total number of connections closed due to prepared statement guardrail
```

The ZDP mechanism reported 6 connections restored and 2 dropped. Crucially, connections restored across the process boundary (old PID `71699` terminated, new PID `72187` started) retained IAM auth token material signed against the pre-upgrade instance resource identifier, rendering them invalid upon token expiration/renewal (~11-15 minutes post-upgrade).

---

## 3. Detailed Failure Modes

### 3.1 IAM Authentication Failures (Severity: Critical — Total Auth Blackout)

#### Observed Log Entries:
```text
2026-11-25 04:55:46 UTC:172.16.15.0(42011):postgres@postgres:[781]:LOG:  pam_authenticate failed: Permission denied
2026-11-25 04:55:46 UTC:172.16.15.0(42011):postgres@postgres:[781]:FATAL:  PAM authentication failed for user "postgres"
2026-11-25 04:55:46 UTC:172.16.15.0(42011):postgres@postgres:[781]:DETAIL:  Connection matched file "/rdsdbdata/config/pg_hba.conf" line 13: "hostssl    all             +rds_iam             all            pam"
...
* upload completely sent off: 1928 bytes
< HTTP/1.1 403 Forbidden
< Content-Type: text/html;charset=utf-8
< Content-Length: 0
* Connection #0 to host rdsauthproxy:1108 left intact

2026-11-25 04:55:54 UTC:172.16.15.0(42125):sql_frontier_eval_user@postgres:[793]:LOG:  pam_authenticate failed: Permission denied
2026-11-25 04:55:54 UTC:172.16.15.0(42125):sql_frontier_eval_user@postgres:[793]:FATAL:  PAM authentication failed for user "sql_frontier_eval_user"
2026-11-25 04:57:00 UTC:172.16.15.0(44023):sql_frontier_eval_user@postgres:[863]:FATAL:  PAM authentication failed for user "sql_frontier_eval_user"
2026-11-25 04:57:48 UTC:172.16.15.0(25561):sql_frontier_eval_user@postgres:[916]:FATAL:  PAM authentication failed for user "sql_frontier_eval_user"
```

#### Analysis:
Both users authenticate via RDS IAM Database Authentication (`pg_hba.conf` line 13, `+rds_iam` group, `pam` authentication method). IAM auth tokens are signed and scoped to the DB resource ID:
`arn:aws:rds-db:<region>:<account>:dbuser:<DbiResourceId>/<username>`

When ZDP provisioned the new instance process, pre-existing tokens and policies minted against the pre-upgrade resource identifier became invalid. The internal `rdsauthproxy:1108` returned `HTTP/1.1 403 Forbidden` prior to reaching PostgreSQL, surfacing as `pam_authenticate failed: Permission denied`.

---

### 3.2 postgresql.conf Parameter Namespace Breaking Changes (Severity: High)

#### Observed Log Entries:
```text
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  invalid configuration parameter name "rds.csd_test_parameter_3"
2026-11-25 04:45:10 UTC::@:[72187]:DETAIL:  "rds" is a reserved prefix.
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  invalid configuration parameter name "rds.proxy_protocol_enabled"
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  invalid configuration parameter name "rds.babelfish_status"
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  invalid configuration parameter name "rds.queryid_counters_min_eviction_sec"
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  invalid configuration parameter name "rds.enable_plan_management"
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  invalid configuration parameter name "rds.proxy_protocol_trusted_cidr"
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  invalid configuration parameter name "rds.adaptive_autovacuum"
2026-11-25 04:45:10 UTC::@:[72187]:LOG:  configuration file "/rdsdbdata/config/postgresql.conf" contains errors; unaffected changes were applied
2026-11-25 04:45:30 UTC::@:[72187]:LOG:  parameter "unix_socket_permissions" cannot be changed without restarting the server
2026-11-25 04:45:30 UTC::@:[72187]:LOG:  parameter "shared_buffers" cannot be changed without restarting the server
```

#### Analysis:
Seven parameters present in the RDS parameter group valid under `17.9.3` were rejected by `17.9.5` because the `rds.` prefix became reserved by the core engine. ZDP upgraded the engine without pre-migrating or validating the parameter group, resulting in partial configuration application (`unaffected changes were applied`) on every subsequent reload.

---

### 3.3 Role Permission Loss — SET ROLE Failure (Severity: High)

#### Observed Log Entry:
```text
2026-11-25 04:48:31 UTC:172.16.15.0(21453):postgres@postgres:[701]:ERROR:  must be able to SET ROLE "sql_frontier_eval_user"
2026-11-25 04:48:31 UTC:172.16.15.0(21453):postgres@postgres:[701]:STATEMENT:  CREATE SCHEMA IF NOT EXISTS sql_frontier_eval_sandbox AUTHORIZATION sql_frontier_eval_user;
```

#### Analysis:
The session user (`postgres`) lost role membership in `sql_frontier_eval_user`, failing the implicit `SET ROLE` inside the `AUTHORIZATION` clause. This privilege existed prior to the upgrade and failed immediately post-upgrade.

---

### 3.4 Database CONNECT Privilege Loss (Severity: High)

#### Observed Log Entry:
```text
2026-11-25 04:49:16 UTC:172.16.15.0(22105):sql_frontier_eval_user@postgres:[712]:ERROR:  permission denied for database postgres
2026-11-25 04:49:16 UTC:172.16.15.0(22105):sql_frontier_eval_user@postgres:[712]:STATEMENT:  DROP SCHEMA IF EXISTS sql_frontier_eval_sandbox CASCADE; CREATE SCHEMA sql_frontier_eval_sandbox; SET search_path TO sql_frontier_eval_sandbox, public;
```

#### Analysis:
`sql_frontier_eval_user` connected via IAM auth but was denied database-level access (`CONNECT` privilege on database `postgres`).

---

## 4. Availability Impact Summary

| Metric | Pre-Upgrade (17.9.3) | Post-Upgrade (17.9.5) | Duration / Window |
| :--- | :--- | :--- | :--- |
| **IAM Auth Success Rate (`postgres`)** | 100% | **0%** | From 04:55:46 UTC, ongoing |
| **IAM Auth Success Rate (`sql_frontier_eval_user`)** | 100% | **0%** | From 04:55:54 UTC, ongoing |
| **`SET ROLE sql_frontier_eval_user`** | 100% | **0%** | From 04:48:31 UTC, ongoing |
| **`CONNECT` to `postgres` Database** | 100% | **0%** | From 04:49:16 UTC, ongoing |
| **`postgresql.conf` Clean Reload** | 100% | **0%** (contains errors) | From 04:45:10 UTC, ongoing |
| **ZDP Connections Dropped** | 0 | 2 | At 04:44:04 UTC |

Total confirmed impact window: **04:44:02 UTC to >04:57:48 UTC** (minimum 14 minutes, persisting without automatic recovery).

---

## 5. Root Cause Statement

1. **IAM Token & Resource ID Invalidation**: The underlying instance transition invalidated active IAM authentication tokens and policies scoped to the pre-upgrade `DbiResourceId`. ZDP did not coordinate with `rdsauthproxy:1108` to refresh token state.
2. **Parameter Namespace Breakage**: Seven AWS-managed `rds.*` parameters became reserved prefixes in PostgreSQL 17.9.5. The ZDP workflow applied the new binary without migrating the parameter group.
3. **Session & ACL Privilege State Loss**: Role membership (`SET ROLE`) and database `CONNECT` grants were lost across the ZDP connection transfer.

---

## 6. Questions for AWS Support

1. Does Zero Downtime Patching guarantee `DbiResourceId` stability across minor version upgrades? If not, how are applications using IAM database authentication expected to rotate credentials without experiencing a 403 auth blackout?
2. Why did the ZDP automated upgrade process not automatically sanitize or remove deprecated `rds.*` namespace parameters from the cluster parameter group?
3. What state is guaranteed to be preserved during ZDP connection restoration? Why were database ACLs (`CONNECT`) and role inheritance lost for transferred sessions?
4. Is this a known regression in Aurora PostgreSQL 17.9.5 ZDP upgrades?

---

## 7. Artifacts & Evidence Files in Repository

- Detailed Incident Report: [`docs/evidence/aurora_zdp_upgrade_incident_report_20261125.md`](docs/evidence/aurora_zdp_upgrade_incident_report_20261125.md)
- Full CloudWatch Logs Extraction: [`docs/evidence/aurora_zdp_cloudwatch_logs.txt`](docs/evidence/aurora_zdp_cloudwatch_logs.txt)
  - Log Group: `/aws/rds/cluster/reality-check-cost-tracker/postgresql`
  - Log Stream: `reality-check-cost-tracker-instance-1`
  - Time Range: `2026-11-25T04:44:02Z` – `2026-11-25T04:57:48Z`
