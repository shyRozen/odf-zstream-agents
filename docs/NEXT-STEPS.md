# ODF Z-Stream — Next Steps

> Roadmap for taking the pipeline from working prototype to production.
> Follows from ODF-ZStream-Multi-Agent-Plan-v2 and ODF-ZStream-Implementation-Notes.

---

## Current State

The pipeline runs end-to-end (`zstream run 4.16.2`) with all 14 nodes, 3 sub-graphs, and 23 tool functions implemented. Without API credentials, it degrades gracefully. The architecture is sound — what remains is integration, hardening, and deployment.

---

## Step 1: Connect Real Credentials

**Priority**: Immediate
**Why**: Nothing works without API access. This is the gate to all downstream testing.

| Credential | Where | How to Get |
|------------|-------|-----------|
| `ANTHROPIC_API_KEY` | `.env` | Anthropic console — needed for all LLM nodes (9 of 14 nodes use an LLM) |
| `JIRA_API_TOKEN` | `.env` | Jira Cloud → Profile → API Tokens. Also set `JIRA_URL` and `JIRA_EMAIL` |
| `JENKINS_API_TOKEN` | `.env` | Jenkins → User → Configure → API Token. Also set `JENKINS_URL` and `JENKINS_USER` |
| `GITHUB_TOKEN` | `.env` | GitHub → Settings → Developer Settings → PAT. Needs repo write access to ocs-ci |
| `SLACK_WEBHOOK_URL` | `.env` | Slack → Apps → Incoming Webhooks → Add to channel |

**Action**: Copy `.env.example` to `.env`, fill in real values, test each tool individually:
```bash
python -c "from tools.jira_tools import jira_search; print(jira_search('4.16.2'))"
python -c "from tools.jenkins_tools import jenkins_get_build_status; print(jenkins_get_build_status('qe-deploy-ocs-cluster-prod', 1))"
```

---

## Step 2: First Real Z-Stream Run

**Priority**: High
**Why**: Validates the entire pipeline with real data. Exposes integration issues that mocks can't catch.

**How**:
1. Pick a recent z-stream that has already been validated manually (so you have ground truth to compare)
2. Run: `zstream run <version>`
3. Compare the Change Manifest against what humans found
4. Compare test selection against what humans ran
5. Compare analysis output against known results

**Expected issues**:
- Jira query may need JQL tuning (field names, project key, status values)
- Errata API endpoint may need adjustment for Red Hat internal access
- ocs-ci repo path may differ if not at `~/codcod/new-ocs-ci/ocs-ci/`
- Mark Matcher relevance scoring needs prompt tuning after seeing real test/change data

**Outcome**: A calibration baseline — what the AI picks vs. what humans would pick. Target: >70% overlap on first run.

---

## Step 3: Prompt Tuning

**Priority**: High
**Why**: LLM nodes ship with generic prompts. Real data reveals what the model misunderstands about your domain.

**Nodes to tune** (in priority order):

### 3a. Mark Matcher (Opus)
The highest-value node — decides which tests run. The prompt needs:
- Examples of what "relevant" means for your team (a ceph-csi fix should trigger PV tests, not MCG tests)
- The scoring rubric (what makes a test score 0.9 vs 0.5?)
- Edge cases (upgrade tests? performance tests? UI tests for backend fixes?)

**How**: Run the pipeline, look at `scored_tests`, identify false positives/negatives, add few-shot examples to the system prompt in `nodes/mark_matcher.py`.

### 3b. Root Cause Analyzer (Opus)
Classifies failures as product_bug / test_bug / infra_issue. Needs:
- Examples of each failure type from your actual logs
- Infra patterns specific to your environment (PSI timeouts, NFS mount failures, etc.)
- Known flaky test patterns

**How**: Feed it 5-10 real failures from past Jenkins runs, check if classifications match human judgment.

### 3c. Merge & Cross-Ref (Sonnet)
Reconciles Jira + errata + git into one manifest. Needs:
- Your Jira field names and conventions
- How errata advisory IDs relate to Jira ticket IDs in your workflow
- Component naming conventions (ceph-csi vs cephcsi vs CSI)

---

## Step 4: PostgreSQL + Checkpointing

**Priority**: Medium
**Why**: Two purposes — (1) pipeline resumes if Jenkins stage takes 4 hours and the process dies, (2) regression detection needs historical data.

**What to build**:

### 4a. LangGraph Checkpointing
```python
from langgraph.checkpoint.postgres import PostgresSaver
checkpointer = PostgresSaver(conn_string=config.POSTGRES_URL)
pipeline = build_pipeline(checkpointer=checkpointer)
```
Each node completion gets checkpointed. If the process crashes during Jenkins polling (3+ hours), restart picks up from the last checkpoint instead of re-running inspect/map/PR stages.

**Why it matters**: Without this, a crash at hour 3 of Jenkins polling wastes the 10 minutes of AI work that already completed.

### 4b. Historical Results DB
The `db_tools.py` already has `save_pipeline_results` and `query_historical_results` with auto-table-creation. Need to:
- Start `docker compose up -d` for PostgreSQL
- Run a few pipelines to populate history
- Verify the regression detector node can query past runs

---

## Step 5: FastAPI Endpoints

**Priority**: Medium
**Why**: The CLI is fine for manual use, but automated triggers (Jira webhooks, errata events) need an API.

**Endpoints**:
```
POST /api/run              → Start pipeline, return pipeline_id
GET  /api/status/{id}      → Current stage, progress, errors
GET  /api/report/{id}      → Full analysis report
GET  /api/history           → List past pipeline runs
POST /api/run/{id}/retry   → Retry from failed stage
```

**Why each endpoint matters**:
- `/run` — Jira or errata webhook calls this automatically when a z-stream ships
- `/status` — Slack bot or dashboard polls this to show progress
- `/report` — Final results page, linked from Slack notifications
- `/retry` — If Jenkins infra was down, retry just the Jenkins + analysis stages

---

## Step 6: Webhook Integration

**Priority**: Medium-Low
**Why**: Eliminates the manual `zstream run` trigger. The pipeline starts automatically when a z-stream release event occurs.

**Options** (pick one to start):

| Trigger | How | Complexity |
|---------|-----|-----------|
| Jira webhook | Configure Jira automation: when issue `fixVersion` is set → POST to `/api/run` | Low |
| Errata webhook | Red Hat errata system notification → POST to `/api/run` | Medium (internal API) |
| Manual Slack command | `/zstream 4.16.2` in Slack → calls API | Low |
| Cron job | Check for new z-stream tags daily → trigger if new | Low |

**Recommendation**: Start with Slack command (easiest to test) + Jira webhook (most useful). Add errata later.

---

## Step 7: LangSmith Observability

**Priority**: Medium-Low
**Why**: Without tracing, debugging a 6-stage pipeline is painful. LangSmith shows node-by-node execution, LLM inputs/outputs, token costs, and latencies.

**Setup**:
```bash
# .env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=odf-zstream
```

**What you get**:
- Visual graph execution trace (which nodes ran, which failed, why)
- LLM prompt/response pairs for every node (debugging hallucinations)
- Token usage and cost per run (is Opus too expensive for mark_matcher?)
- Latency breakdown (which node is the bottleneck?)

---

## Step 8: Kubernetes Deployment

**Priority**: Low (production hardening)
**Why**: Docker Compose is fine for single-server use. K8s needed if the team wants the API always available, auto-restart on failure, and horizontal scaling.

**What to create**:
- `k8s/deployment.yaml` — Pipeline API pod
- `k8s/postgres.yaml` — PostgreSQL StatefulSet (or use managed DB)
- `k8s/configmap.yaml` — config.yaml
- `k8s/secret.yaml` — API keys
- Health check endpoint: `GET /api/health`

**When to do this**: After Step 5 (API) is stable and you've run 5+ real z-streams through the pipeline.

---

## Step 9: Production Polish

**Priority**: Low (after 5+ successful runs)
**Why**: These make the system reliable for the team to depend on daily.

| Item | Why |
|------|-----|
| Rate limiting for LLM APIs | Anthropic has rate limits. Add retry+backoff in `core/llm.py` |
| Secrets management (Vault) | `.env` files are fragile. Move to HashiCorp Vault or K8s secrets |
| `auto_file_bugs: true` | Once root cause accuracy exceeds 80%, enable auto-bug-filing in Jira |
| CI/CD for the agent itself | GitHub Actions to test the pipeline code on PRs |
| Runbook | Document: how to trigger, what to check, how to debug failures |
| Team onboarding | 30-min demo + walkthrough for QE team |

---

## Timeline Estimate

| Step | Effort | Dependency |
|------|--------|-----------|
| 1. Credentials | 1 hour | None |
| 2. First real run | 2-3 hours | Step 1 |
| 3. Prompt tuning | 1-2 days | Step 2 (need real data) |
| 4. PostgreSQL + checkpointing | 1 day | None |
| 5. FastAPI endpoints | 1-2 days | Step 4 |
| 6. Webhook integration | 1 day | Step 5 |
| 7. LangSmith | 2 hours | None |
| 8. Kubernetes | 2-3 days | Step 5 |
| 9. Production polish | Ongoing | Step 8 |

**First usable pipeline**: Steps 1-3 (~2-3 days)
**Production-ready**: Steps 1-7 (~2 weeks)
**Enterprise-grade**: All steps (~4 weeks)
