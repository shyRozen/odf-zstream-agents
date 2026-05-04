# ODF Jenkins Infrastructure Reference

> Comprehensive reference for the ocs4-jenkins repository at `~/codcod/ocs4-jenkins-fork/`.
> This document captures everything needed for the ODF-ZStream-Multi-Agent-Plan agent integration.

---

## Repository Structure

```
ocs4-jenkins-fork/
├── jobs/                          # 51 Job DSL files + 52 pipeline directories
│   ├── *.groovy                   # Job DSL definitions
│   ├── pipelines/*/Jenkinsfile    # Pipeline scripts per job
│   └── main_seed_job.groovy       # Seed job: processes all .groovy → generates jobs/views
│
├── src/main/groovy/ocsJobLib/
│   └── parameters.groovy          # 2585 lines — all parameter definitions
│
├── vars/                          # 131 shared library functions (global vars)
├── views/                         # Jenkins view definitions (tier, feature, platform)
├── scripts/                       # Python/Bash helper scripts
├── ocs-ci-config/                 # Cluster & ocs-ci configs
├── ansible/                       # Playbooks (jslave, NFS mount, CA, proxy)
├── terraform/                     # IaC (OpenStack, vSphere, IBM Cloud)
├── dockerfiles/                   # Jenkins sidecar image (UBI9, Python 3.11, oc/kubectl)
├── casc.yaml                      # Jenkins Configuration as Code (40KB — credentials, plugins)
├── casc-init.yaml                 # CasC init (28KB)
├── properties.yaml                # Jenkins CSB properties
└── plugins.txt                    # Required plugins list
```

---

## Jenkins Environments

| Environment | URL | Purpose |
|-------------|-----|---------|
| **Production** | `https://jenkins-csb-odf-qe-ocs4.dno.corp.redhat.com` | Live CI runs |
| **Stage** | `https://jenkins-csb-odf-qe-stage.dno.corp.redhat.com` | Testing changes to Jenkins infra |
| **Resource Locker** | `https://odf-resourcelocker.apps.int.spoke.prod.us-east-1.aws.paas.redhat.com` | Cluster allocation service |
| **Logs/Artifacts** | `http://magna002.ceph.redhat.com/ocsci-jenkins/` | NFS-hosted test logs |
| **Backup Logs** | `http://odf-ci-nfs-backup.usersys.redhat.com/` | Log backups |

Detection of production vs stage:
```groovy
// vars/constants.groovy
isProdJenkins = env.JENKINS_URL.contains('jenkins-csb-odf-qe-ocs4')
```

---

## Job Definitions

### Job DSL Pattern

All jobs defined in `jobs/*.groovy` using Jenkins Job DSL plugin. The seed job (`jobs/main_seed_job.groovy`) processes all `.groovy` files to generate jobs and views.

### Key Job Variants

**Deployment + Test Jobs:**

| Job Name | File | Retention | Purpose |
|----------|------|-----------|---------|
| `qe-deploy-ocs-cluster` | `qe_deploy_ocs_cluster.groovy` | 6 days | QE manual deploy + test |
| `eng-deploy-ocs-cluster` | same file | 4 days | Engineering variant |
| `qe-deploy-ocs-cluster-prod` | same file | ODF_LIFECYCLE | **Production CI — best target for agent** |
| `qe-deploy-fdf-cluster` | same file | 6 days | Fusion Data Foundation |
| `qe-deploy-fdf-cluster-prod` | same file | ODF_LIFECYCLE | FDF production |
| `qe-deploy-ocs-cluster-multi` | same file | 6 days | Multicluster |
| `qe-deploy-ocs-cluster-multi-prod` | same file | ODF_LIFECYCLE | Multicluster production |

**Nightly Jobs:**

| Job Name | File | Purpose |
|----------|------|---------|
| `qe-trigger-nightly-jobs-cloud` | `qe_trigger_nightly_ci.groovy` | Cloud platform nightlies |
| `qe-trigger-nightly-jobs-on-prem` | same file | On-prem nightlies |

Cloud nightlies include:
- Tier1 AWS IPI RHCOS KMS Thales
- Tier4a AWS IPI RHCOS KMS Thales
- Tier4c AWS IPI RHCOS KMS Thales
- Acceptance GCP IPI RHCOS Shielded Machines
- Azure Encryption Key Vault IPI RHCOS

**Other Jobs:**
- `qe-destroy-ocs-cluster` — Cluster teardown
- `qe-trigger-test-pr` — PR testing
- `qe-ocs-ci-stage-testing-pipeline` — Stage testing
- `qe-multicluster` — Multi-cluster deployments
- `qe-dr-setup` / `qe-mdr-setup` / `qe-rdr-setup` — Disaster Recovery
- `qe-recreate-temp-agent` — Temp agent management
- `qe-aws-cleanup` — AWS resource cleanup

---

## Pipeline Structure (Main Jenkinsfile)

**File**: `jobs/pipelines/qe_deploy_ocs_cluster/Jenkinsfile` (1206 lines)

```
Pipeline Flow:
┌─────────────────────┐
│  Initialization     │  sharedVars(), setAnalysisRotationAssignee(),
│  (Lines 13-111)     │  deployJobInitChecks(), reporter(RUNNING)
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Prepare JSlave     │  Terraform temp agent, mount cluster dirs,
│  (Lines 113-169)    │  3 retry attempts
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Install OCP        │  run-ci with OCP deployment params,
│  (Lines 187-246)    │  conditional on RUN_INSTALL_OCP
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Deploy EDR         │  Conditional: Azure OR DEPLOY_EDR=true,
│  (Lines 248-282)    │  non-blocking failure
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Install OCS        │  run-ci with OCS deployment params,
│  (Lines 289-380)    │  createCustomConfig(), conditional on RUN_INSTALL_OCS
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Upgrade            │  Conditional: UPGRADE=true,
│  (Lines 390-450)    │  runUpgrade(vars), supports multi-iteration
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Test Execution     │  run-ci with TEST_MARK_EXPRESSION,
│  (Lines 441-576)    │  JUnit XML + HTML report + Jira + squad-analysis
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  ITR Execution      │  Optional: acceptance + ITR enabled,
│  (Lines 497-537)    │  parallel workers, retry, merged JUnit
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Lib Tests          │  Conditional: RUN_LIB_TEST=true,
│  (Lines 578-649)    │  LIB_TEST_MARK_EXPRESSION
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Post Always        │  post_always(vars), IRC notification
│  (Lines 651-656)    │
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Post Failure       │  console_log.py parsing, error reports,
│  (Lines 658-729)    │  email with analysis rotation, reporter update
└─────────────────────┘
```

### Test Execution Command (exact format)

```bash
run-ci \
    --color=yes \
    ${params.TEST_PATH} \
    -m '${params.TEST_MARK_EXPRESSION}' \
    -k '${params.TEST_NAME_EXPRESSION}' \
    ${vars.ocs_version} \
    ${vars.live_deploy_param} \
    ${vars.ocs_registry_image} \
    --junit-xml ${vars.testResultsFile} \
    -o junit_suite_name=${vars.junitSuiteName} \
    --html=~/current-cluster-dir/logs/test_report_${vars.timestamp}.html \
    --jira \
    --squad-analysis \
    ${vars.email_arg} \
    ${params.ADDITIONAL_PYTEST_PARAMS}
```

### Upgrade Execution Command

```bash
run-ci \
    --color=yes \
    ${params.UPGRADE_TEST_PATH} \
    -m '${params.UPGRADE_TEST_MARK_EXPRESSION}' \
    --junit-xml ${vars.upgradeResultsFile} \
    ... (same flags as test)
```

---

## All Job Parameters

**Source**: `src/main/groovy/ocsJobLib/parameters.groovy` (2585 lines)

### Cluster Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `CLUSTER_NAME` | String | `''` | Full cluster name |
| `CLUSTER_PREFIX` | String | varies | Prefix for auto-generated names |
| `CLOUD_NAME` | Choice | `''` | Cloud selection: `rhos-d`, `rhos-01` |
| `LOCKABLE_RESOURCE` | String | `''` | Resource lock name |
| `IGNORE_LOCK` | Boolean | `false` | Skip resource locking |
| `LOCK_PRIORITY` | Choice | `3` | Lock priority (1=highest, 5=lowest) |

### Version Parameters

| Parameter | Type | Default | Options |
|-----------|------|---------|---------|
| `OCP_VERSION` | Choice | `''` | 4.22, 4.21, 4.21-ga, 4.20, 4.20-ga, 4.19-ga, 4.19, 4.18-ga, 4.18, ... 4.11 |
| `OCS_VERSION` | Choice | `''` | 4.22, 4.21, 4.20, 4.19, 4.18, 4.17, 4.16, 4.15, 4.14, 4.14-eus, ... 4.11 |
| `UPGRADE_OCP_VERSION` | Choice | `''` | Same as OCP_VERSION |
| `UPGRADE_OCS_VERSION` | Choice | `''` | Same as OCS_VERSION |
| `OCS_REGISTRY_IMAGE` | String | `''` | Registry image for OCS deployment |
| `UPGRADE_OCS_REGISTRY_IMAGE` | String | `''` | Registry image for upgrade |
| `OCP_INSTALLER_VERSION` | String | `''` | OCP installer override |

### Workflow Control Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `RUN_PREPARE_JSLAVE` | Boolean | `true` | Provision temporary Jenkins agent |
| `RUN_INSTALL_OCP` | Boolean | `true` | Install OpenShift cluster |
| `RUN_INSTALL_OCS` | Boolean | `true` | Install OCS/ODF |
| `RUN_TEST` | Boolean | `true` | Execute test stage |
| `RUN_LIB_TEST` | Boolean | `false` | Execute library tests |
| `RUN_TEARDOWN` | Boolean | `true` | Destroy cluster after |
| `UPGRADE` | Boolean | `false` | Run upgrade workflow |
| `PAUSE_BEFORE_UPGRADE` | Boolean | `false` | Manual gate before upgrade |
| `PAUSE_BEFORE_TEST_EXECUTION` | Boolean | `false` | Manual gate before tests |
| `PAUSE_BEFORE_TEARDOWN` | Boolean | `false` | Manual gate before teardown |

### Test Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `TEST_PATH` | String | `tests/` | Test directory or file path |
| `TEST_MARK_EXPRESSION` | String | `tier1` | Pytest `-m` marker filter |
| `TEST_NAME_EXPRESSION` | String | `''` | Pytest `-k` name filter |
| `UPGRADE_TEST_PATH` | String | `tests/` | Upgrade test path |
| `UPGRADE_TEST_MARK_EXPRESSION` | String | `pre_upgrade or pre_ocs_upgrade or ocs_upgrade or post_ocs_upgrade or post_upgrade` | Upgrade markers |
| `LIB_TEST_PATH` | String | `tests/` | Library test path |
| `LIB_TEST_MARK_EXPRESSION` | String | `libtest` | Library test markers |
| `ADDITIONAL_PYTEST_PARAMS` | String | `''` | Extra raw pytest args |
| `RE_TRIGGER_FAILED_TESTS` | String | `''` | XML path for re-triggering failures |
| `AUTOMATIC_RE_TRIGGER_FAILED_TESTS` | Boolean | `false` | Auto-retry failed tests |

### Platform Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `CLUSTER_CONF` | String | varies | Space-separated config file list |
| `YAML_TEXT_CONFIG` | Text | `''` | Dynamic YAML config injection |
| `LIVE_DEPLOYMENT` | Boolean | `false` | Deploy from live content |
| `UI_DEPLOY` | Boolean | `false` | UI-based deployment |
| `DEPLOY_EDR` | Boolean | `false` | Deploy EDR |

### Reporting Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `EMAIL` | String | `''` | Notification email |
| `PRODUCTION_RUN` | Boolean | `false` | Enable production reporting |
| `DISPLAY_NAME` | String | `''` | Google Sheets run name |
| `REPORT_PORTAL` | Boolean | `true` | Upload to ReportPortal |
| `REPORT_PORTAL_PROJECT` | String | `odf-qe` | ReportPortal project |
| `COLLECT_LOGS` | Boolean | `true` | Collect must-gather logs |
| `COLLECT_LOGS_ON_SUCCESS` | Boolean | `false` | Logs even on success |
| `FULL_ERRORS` | Boolean | `true` | Full error in email |
| `TRUNCATED_ERRORS` | Boolean | `true` | Truncated error summary |
| `ERROR_LINES` | String | `40` | Lines per error in email |
| `SKIP_REPORTER_SCRIPT` | Boolean | `false` | Skip Google Sheets |

### ITR Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ITR_WORKERS_COUNT` | String | `3` | Parallel ITR workers |
| `ITR_PAYLOAD_RETRY` | String | `1` | Retry count per payload |

### MCG Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `RUN_MCG_FOCUSED_BUILD` | Boolean | `false` | Filter tests to MCG only |

---

## Shared Library Functions (vars/)

### Test Execution

**`prepareRunCiArgs(vars, injectEmailAnalysis=true, junit=true)`**
File: `vars/prepareRunCiArgs.groovy` (65 lines)

Builds the `run-ci` argument list:
- `--color=yes`
- JUnit XML path (switches between test/upgrade based on stage)
- HTML report path
- Email args with optional analysis rotation injection
- Config file arguments from `CLUSTER_CONF`
- Custom YAML config support

**`runTestSuiteJob(jobName, jobParameters)`**
File: `vars/runTestSuiteJob.groovy` (13 lines)

Wrapper for downstream job execution:
- Builds job with parameters
- Returns job object
- Updates build description with link + result

**`generateJobStages(jobs, runParallel=false, startFromJob='', pauseOnFailure=true, subjectAppendix='', email='ocs-ci@redhat.com', timeout_time=0)`**
File: `vars/generateJobStages.groovy` (105 lines)

Job stage orchestration:
- Creates serial or parallel stage execution
- Supports `skipExpression` for conditional skipping with reasons
- Calls `runTestSuiteJob` or `runTestSuiteJobAndPauseOnFailure`
- Returns list of downstream job objects

**`automaticReTriggerFailedTests(vars, currentBuild)`**
File: `vars/automaticReTriggerFailedTests.groovy` (57 lines)

Automatic retry of failed tests:
1. Extracts JUnit XML from build artifacts
2. Parses failure/error/skipped counts via `xmllint`
3. If failures exist: retrieves original build parameters
4. Re-triggers upstream job with `RE_TRIGGER_FAILED_TESTS` set to the XML path
5. Appends re-triggered job link to build description

**`getMCGOnlyTestMarkExpression(testMarkExpression)`**
File: `vars/getMCGOnlyTestMarkExpression.groovy` (16 lines)

Modifies marker expression for MCG-focused builds:
- If `params.RUN_MCG_FOCUSED_BUILD` = true
- Returns: `"(${testMarkExpression}) and mcg"`

**`getTestReportResults(currentBuild, junitObject)`**
File: `vars/getTestReportResults.groovy` (34 lines)

Extracts test metrics from JUnit:
- `successRate = (passCount / (totalCount - skipCount)) * 100`
- Duration in hours and minutes
- Counts: total, passed, failed, skipped

**`prepareForITRRun()`**
File: `vars/prepareForITRRun.groovy` (58 lines)

ITR (Intelligent Test Runner) setup:
- Downloads ITR binary from `http://ocsqe-httpdserver.usersys.redhat.com/ocs4qe/itr`
- Downloads test case list: `acceptance_positive_test_cases`
- Copies kubeconfig and configs
- Clears OS cache: `sudo sh -c 'echo 1 > /proc/sys/vm/drop_caches'`

### Initialization & Configuration

**`sharedVars(vars=null)`**
File: `vars/sharedVars.groovy` (133 lines)

Pipeline variable initialization:
- Cluster naming: `fullClusterName`, `truncatedClusterName` (max 17 chars)
- Test paths: `testResultsFile`, `upgradeResultsFile`, `libTestResultsFile`
- OCS/OCP versions from params
- Email and log paths
- Test result map for reporter
- Performance profile arguments
- Client HTTP proxy settings

**`commonSharedVars(vars=null)`**
File: `vars/commonSharedVars.groovy` (53 lines)

Common variables:
```groovy
releasedOdfVersions = ['4.14', '4.15', '4.16', '4.17', '4.18', '4.19', '4.20']
python = 'python3.11'
```

RHCS Support Matrix (OCP → RHCS version mapping):
```groovy
// OCP 4.14-4.16 → RHCS 6.1
// OCP 4.17-4.18 → RHCS 7.1
// OCP 4.19-4.20 → RHCS 8.0
// OCP 4.21-4.22 → RHCS 8.0
```

**`constants.groovy`**
File: `vars/constants.groovy` (21 lines)

```groovy
prodUrl = 'https://jenkins-csb-odf-qe-ocs4.dno.corp.redhat.com'
stageUrl = 'https://jenkins-csb-odf-qe-stage.dno.corp.redhat.com'
resourceLockerBaseUrl = 'https://odf-resourcelocker.apps.int.spoke.prod.us-east-1.aws.paas.redhat.com'
isProdJenkins = env.JENKINS_URL.contains('jenkins-csb-odf-qe-ocs4')
```

**`createCustomConfig(vars, ...)`**
File: `vars/createCustomConfig.groovy` (103 lines)

Generates `ocsci_conf.yaml` from template:
- Sets Jenkins build URL and logs URL in `RUN` section
- Sets `UI_DEPLOY` if `params.UI_DEPLOY=true`
- Sets client HTTP proxy if needed
- Sets `UPGRADE` flag if `params.UPGRADE=true`
- Injects `build_user`, `primary_assignee`, `backup_assignee` for production runs
- Generates ReportPortal launch URL for logs
- Output: `${HOME}/current-cluster-dir/ocsci_conf.yaml`

### Reporting

**`reporter(params, reportData, vars)`**
File: `vars/reporter.groovy` (67 lines)

Google Sheets integration:
- **Condition**: Only runs if `PRODUCTION_RUN=true` AND `DISPLAY_NAME` set AND `ocsBuild` exists
- Clones jobs repo (15 retry attempts)
- Extracts OCS build from `OCS_REGISTRY_IMAGE` or `UPGRADE_OCS_REGISTRY_IMAGE`
- Executes `scripts/python/reporter.py` with data map as arguments
- Uses Python 3.11 venv in `.reporterVenv`

Google Sheets details:
- API key: `~/.ocs-ci/google_api_secret.json`
- Sheet: `https://docs.google.com/spreadsheets/d/13Z779BW63btcAJoZSutDsAdBbCxvCrLIy-J2ou5r_dI`
- Headers: Jenkins build, OCP version, Markers, Status (RUNNING/PASSED/FAILED), Success Rate (%)
- Retry: 20 attempts with 61s delay for API errors

**`reportPortalUpload(report, logs_dir, output_file=null, email=null, ignoreFailure=true)`**
File: `vars/reportPortalUpload.groovy` (141 lines)

ReportPortal via Data Router:
1. Checks for existing reports (dedup)
2. Downloads Data Router client (default: `latest`)
3. Config: `datarouter_template.json` + `datarouter_odf_qe.yaml` credentials
4. Launch properties parsed: `rp_launch_name`, `rp_launch_description`, squad failure attributes
5. Purple analysis filtering: only include assignee attributes if "Purple-analysis-needed" present
6. Executes Data Router droute client
7. Output: YAML with launch ID, redirect HTML from template

**`sendFailureEmailReport(vars, ...)`**
File: `vars/sendFailureEmailReport.groovy`

Sends failure notification emails:
- Includes analysis rotation assignees for production runs
- Parses console log for truncated/full error summaries
- Links to build, logs, ReportPortal

**`slackMessage(message, channel='#odf-qe-ci-notifications', credentials='slack-webhooks')`**
File: `vars/slackMessage.groovy` (43 lines)

Slack webhook notifications:
- Reads webhook URLs from YAML credentials
- Posts JSON message to channel webhook
- Validates response = 'ok'

### Resource Management

**`resourceManagement(lockable_resource_data, action, emailAddress)`**
File: `vars/resourceManagement.groovy`

Resource Locker API client:
- Base URL: `https://odf-resourcelocker.apps.int.spoke.prod.us-east-1.aws.paas.redhat.com`
- Actions: `lock`, `release`, `check`
- Data fields: `signoff` (identifier), `search_string` (resource label), `priority`, `link`
- Retry: 20s × 8640 attempts = ~48 hours max wait
- Health check: 5 attempts before pause for user input
- Slack notification for waiting requests after N minutes

### Version Handling

**`parseRelativeVersion(version, baseVersion)`**
File: `vars/parseRelativeVersion.groovy` (17 lines)

Parses relative versions (`n-1`, `n-2`):
```groovy
def matcher = (version =~ /n-(\d)/)
if (matcher.matches()) { return computed_version }
return version  // pass through if not relative
```

**`compareVersion(version1, version2)`**
File: `vars/compareVersion.groovy`

Semantic version comparison.

**`previousVersion(version, versionStep=1)`**
File: `vars/previousVersion.groovy`

Returns previous version (e.g., 4.16 → 4.15).

**`setOcsBuild(vars)`**
File: `vars/setOcsBuild.groovy`

Determines OCS build version from registry image parameter.

### Cluster Operations

**`downloadClusterArchive(vars)`** — Download cluster artifacts from NFS
**`mountClusterDirs(vars)`** — Mount NFS cluster directories
**`getClusterDirFullPath(vars)`** — Compute cluster directory path
**`checkForExistingVm(vars)`** — Check VM existence in cloud

### Utility Functions

**`convertMapToArguments(map)`** — Map → CLI arguments
**`getReportPortalLinks(vars, ...)`** — Generate RP links
**`shortUrl(longUrl)`** — URL shortener
**`jsonPrettyPrint(jsonString)`** — JSON formatting
**`randomStringGenerator(length)`** — Random string generation
**`setBuildDescription()`** — Update Jenkins build description

---

## Platform Configurations

### Supported Platforms (from parameters.groovy)

**AWS:**
| Config | Path |
|--------|------|
| AWS IPI | `ocsci/aws_ipi.yaml` |
| AWS UPI | `ocsci/aws_upi.yaml` |
| AWS IPI 3AZ RHCOS 3M 3W | `deployment/aws/ipi_3az_rhcos_3m_3w.yaml` |
| AWS IPI 1AZ + LSO | `deployment/aws/ipi_1az_rhcos_lso_3m_3w.yaml` |
| AWS UPI 3AZ | `deployment/aws/upi_3az_rhcos_3m_3w.yaml` |

**Azure:**
| Config | Path |
|--------|------|
| Azure IPI 3AZ | `deployment/azure/ipi_3az_rhcos_3m_3w.yaml` |
| Azure Encryption | `deployment/azure/ipi_3az_rhcos_encryption_3m_3w.yaml` |
| Azure Perfplus | `deployment/azure/ipi_3az_rhcos_perfplus_3m_3w.yaml` |

**vSphere:**
| Config | Path |
|--------|------|
| vSAN | `deployment/vsphere/ipi_1az_rhcos_vsan_3m_3w.yaml` |
| VMFS | `deployment/vsphere/ipi_1az_rhcos_vmfs_3m_3w.yaml` |
| LSO | `deployment/vsphere/ipi_1az_rhcos_lso_3m_3w.yaml` |
| External Vault | `deployment/vsphere/ipi_1az_rhcos_vsan_external_vault_3m_3w.yaml` |
| Compact | `deployment/vsphere/ipi_1az_rhcos_vsan_compact_3m_0w.yaml` |
| IPv6 | (standalone-ipv6 terraform module) |

**GCP:**
| Config | Path |
|--------|------|
| GCP IPI | `deployment/gcp/ipi_...` |
| GCP Shielded | GCP shielded machines variant |

**IBM Cloud:**
| Config | Path |
|--------|------|
| IBM Managed | `deployment/ibmcloud/managed_...` |
| IBM Custom VPC | `deployment/ibmcloud/custom_vpc_...` |

**Baremetal:**
| Config | Path |
|--------|------|
| AI Install | `deployment/baremetal/ai_...` |
| UPI NVMe Intel | `deployment/baremetal/upi_1az_rhcos_nvme_intel_3m_3w.yaml` |

**Managed Services:**
| Config | Path |
|--------|------|
| ROSA | `deployment/rosa/...` |
| ROSA HCP | HCP (Hosted Control Plane) variants |

---

## Credential Configurations (from casc.yaml)

| Type | Count | Purpose |
|------|-------|---------|
| OpenStack v2/v3 | Multiple | PSI RHOS-D cloud access |
| SSH Keys | 2+ | `ocs4-jenkins`, `ocs-ci` |
| GitHub Tokens | 1+ | ocs-ci account access |
| Quay.io | Multiple | Registry access |
| Polarion | 1 | Test case management |
| vSphere | 19+ | DC1-DC19, multiple clusters |
| Azure | Multiple | Azure subscription access |
| IBM Cloud | Multiple | IBM Cloud API |
| Vault/KMS | Multiple | Vault v1, v2, Thales |
| Ansible Vault | 1 | Encrypted ansible vars |
| Pull Secrets | 1+ | OpenShift pull secrets |

---

## Reporting Stack

### Google Sheets Reporter

- **Script**: `scripts/python/reporter.py`
- **API key**: `~/.ocs-ci/google_api_secret.json`
- **Spreadsheet**: `https://docs.google.com/spreadsheets/d/13Z779BW63btcAJoZSutDsAdBbCxvCrLIy-J2ou5r_dI`
- **Condition**: `PRODUCTION_RUN=true` AND `DISPLAY_NAME` set AND `ocsBuild` exists
- **Data fields**: Jenkins build, OCP version, Markers, Status, Success Rate
- **Retry**: 20 attempts, 61s delay

### ReportPortal

- **Upload script**: `scripts/python/report_portal/rp_upload.py`
- **Config**: `datarouter_template.json` + `datarouter_odf_qe.yaml`
- **Client**: Data Router droute (downloaded at runtime, default `latest`)
- **Launch properties**: name, description, squad failure attributes
- **Dedup**: Checks for existing reports before uploading
- **Output**: YAML with launch ID + redirect HTML

### Console Log Analysis

- **Script**: `scripts/python/console_log.py`
- **Purpose**: Parses Jenkins console log for error extraction
- **Output modes**: `FULL_ERRORS`, `TRUNCATED_ERRORS`, `ERROR_LINES` (configurable)
- **Used in**: Post-failure email reports

### Purple Analysis

- **Script**: `scripts/python/rp_purple_analysis.py`
- **Purpose**: Test analysis and categorization for squad attribution
- **Integration**: ReportPortal launch attributes for "{squad}-failures" and "{squad}-analysis-needed"

### Analysis Rotation

- **Variables** (from `commonSharedVars.groovy`):
  - `vars.analysisAssignee`: `"PrimaryName,BackupName"`
  - `vars.analysisAssigneeEmails`: `"primary@rh,primary@ibm,backup@rh,backup@ibm"`
- **Injection**: Into email notifications, ReportPortal attributes, Google Sheets
- **Function**: `setAnalysisRotationAssignee(vars)` — reads from external rotation schedule

---

## Test Result Artifacts

### File Naming Convention

```
${clusterDirPath}/logs/test_results_${timestamp}.xml          # Main test JUnit
${clusterDirPath}/logs/upgrade_test_results_${timestamp}.xml   # Upgrade test JUnit
${clusterDirPath}/logs/lib_test_results_${timestamp}.xml       # Lib test JUnit
${clusterDirPath}/logs/itr_test_results_${timestamp}.xml       # ITR test JUnit
${clusterDirPath}/logs/test_report_${timestamp}.html           # HTML report
${clusterDirPath}/logs/report_portal/rp_datarouter_output_${timestamp}.txt  # RP output
```

### ReportPortal Launch URL Pattern

```
http://magna002.ceph.redhat.com${logPath}/logs/${stageName}_rp_launch_redirect_${timestamp}.html
```

---

## Views Organization

**File**: `views/infra_view.groovy`

| View | Regex | Purpose |
|------|-------|---------|
| Tier1 | `(?!.*old.*).*tier1(?!.*-nightly.*).*` | Tier 1 test jobs |
| Tier2 | `.*tier2.*` | Tier 2 test jobs |
| Tier3 | `.*tier3.*` | Tier 3 test jobs |
| Tier4 | `.*tier4.*` | Tier 4 test jobs |
| Acceptance | `.*acceptance.*` | Acceptance tests |
| Performance | `.*performance.*` | Performance tests |
| Scale | `.*scale.*` | Scale tests |
| Deployment | `.*deploy.*` | Deployment jobs |
| Upgrade-OCS | `.*upgrade-ocs.*` | OCS upgrade jobs |
| Upgrade-OCP | `.*upgrade-ocp.*` | OCP upgrade jobs |
| DR | `.*dr.*` | Disaster recovery |
| MS | `.*managed.*` | Managed service |
| ROSA | `.*rosa.*` | ROSA jobs |
| Infrastructure | `.*infra.*` | Infrastructure mgmt |

---

## Infrastructure as Code

### Terraform

| Provider | Location | Purpose |
|----------|----------|---------|
| OpenStack | `terraform/openstack/main.tf` | PSI RHOS-D (temp Jenkins agents) |
| vSphere standalone | `terraform/ceph/vsphere/standalone/main.tf` | Ceph on vSphere |
| vSphere stretched | `terraform/ceph/vsphere/stretched/main.tf` | Stretched Ceph cluster |
| vSphere IPv6 | `terraform/ceph/vsphere/standalone-ipv6/main.tf` | IPv6 variant |
| IBM Cloud | `terraform/ceph/ibm/standalone/main.tf` | IBM bare metal + VSI |

Terraform var files: `rhos-d.tfvars`, `rhos-01.tfvars`

### Ansible

| Playbook | Purpose |
|----------|---------|
| `ansible/jenkins-slave/playbook.yml` | Temp agent provisioning |
| `ansible/mount-cluster-dirs/` | NFS cluster dir mounting |
| `ansible/certification-authority/` | CA setup |
| `ansible/aws-proxy/` | AWS proxy config |

### Docker

**Sidecar image** (`dockerfiles/jenkins/sidecars/Dockerfile`):
- Base: UBI9
- Python 3.11, Git, GCC, OpenSSL, LibXML2
- Kubernetes CLIs: `kubectl`, `oc`
- `yq` (YAML processor)
- IBM Cloud CLI with plugins
- `git-crypt` for encrypted files

---

## Agent Integration Points Summary

For the ODF-ZStream-Multi-Agent-Plan, the agent system needs to interact with:

### API Endpoints

```python
# Trigger build
POST {JENKINS_URL}/job/qe-deploy-ocs-cluster-prod/buildWithParameters

# Get queue item → build number
GET {JENKINS_URL}/queue/item/{queue_id}/api/json

# Poll build status
GET {JENKINS_URL}/job/qe-deploy-ocs-cluster-prod/{build_number}/api/json

# Get JUnit test results
GET {JENKINS_URL}/job/qe-deploy-ocs-cluster-prod/{build_number}/testReport/api/json

# Get console log
GET {JENKINS_URL}/job/qe-deploy-ocs-cluster-prod/{build_number}/consoleText

# Get artifacts
GET {JENKINS_URL}/job/qe-deploy-ocs-cluster-prod/{build_number}/artifact/{path}

# Resource Locker
GET/POST {RESOURCE_LOCKER_URL}/api/...
```

### Critical Parameters for Z-Stream Runs

```
TEST_MARK_EXPRESSION = "zstream_4_16_2"     # The custom mark
TEST_PATH            = "tests/"              # Default
OCS_VERSION          = "4.16"                # Major.minor
OCP_VERSION          = "4.16"                # Matching OCP
RUN_INSTALL_OCP      = false                 # Reuse cluster
RUN_INSTALL_OCS      = false                 # Reuse cluster
RUN_TEST             = true                  # Run tests
RUN_TEARDOWN         = false                 # Keep for reruns
PRODUCTION_RUN       = true                  # Enable reporting
REPORT_PORTAL        = true                  # Upload results
EMAIL                = "team@redhat.com"     # Notifications
```

### Functions NOT to Duplicate

The agent should trigger Jenkins jobs that already use these — don't reimplement:

| Function | Why |
|----------|-----|
| `prepareRunCiArgs()` | Already builds the exact `run-ci` command |
| `automaticReTriggerFailedTests()` | Already handles retry logic |
| `reporter()` | Already reports to Google Sheets |
| `reportPortalUpload()` | Already uploads to ReportPortal |
| `slackMessage()` | Already sends Slack notifications |
| `createCustomConfig()` | Already generates ocsci config |
| `resourceManagement()` | Already handles cluster locking |

The agent adds value by **deciding what to run** (test selection), not by reimplementing **how to run** it.