# Lane C Z-Stream Optimization Strategy

> Related: [[ODF-ZStream-Multi-Agent-Plan-v2]] | [[ODF-ZStream-Implementation-Notes]] | [[ODF-ZStream-Next-Steps]]

## Problem Statement

ODF supports 7-8 versions simultaneously (currently 4.14-4.21), with 5-6 under full support (Term 1) receiving monthly Lane C z-streams. Each Lane C contains 10-30 bug fixes and requires extensive regression testing. This is the largest overhead on the team and infrastructure.

### Key Pain Points

- **Per-fix cluster provisioning**: Each fix currently gets its own cluster for verification. N fixes = N clusters (or more for complex topologies).
- **Complex topologies**: Fixes in Regional DR, Metro DR, Provider-Client, and External mode require high-footprint multi-cluster setups.
- **No systematic test selection**: No proven method to scope regression tests to only what's relevant per release — the full suite runs every time.
- **Multi-version repetition**: Similar work repeated across 5-6 versions with minor differences.
- **Manual verification**: Each fix is manually verified by an engineer who reproduces the bug, confirms the fix, and runs surrounding regression tests (2-4 hours per fix).

### Goal

Reduce team time and infrastructure consumption on Lane C while maintaining qualification quality.

---

## Strategy Overview

The strategy is built on four pillars that work together as an integrated pipeline:

| Pillar | Focus | Key Outcome |
|--------|-------|-------------|
| 1 | Regression-Then-Verify (Cluster Reuse) | Zero additional clusters for fix verification |
| 2 | AI-Driven Test Selection | Run 30-50% of tests instead of 100% |
| 3 | AI-Powered Fix Verification Agent | Reduce per-fix verification from hours to minutes |
| 4 | Multi-Version Efficiency | Verify once, qualify many |

---

## Pillar 1: Regression-Then-Verify — "Reuse the Regression Cluster"

### Current State

Regression and fix verification run on separate clusters:
```
Regression clusters (per topology) → regression runs → torn down
Verification clusters (per fix)    → manual verification → torn down separately
```

### Proposed Model

Eliminate dedicated verification clusters entirely. After regression completes on a cluster, keep it running and let the AI agent verify all fixes relevant to that topology on the same cluster:

```
Regression clusters (per topology) → regression runs → cluster stays up
                                                      → AI agent verifies fixes for that topology
                                                      → then tear down
```

### Why This Works

1. **Zero additional clusters for verification** — the infrastructure is already provisioned and warm.
2. **Cluster is in a known-good state** — regression just passed, so baseline health is confirmed.
3. **No separate "shared topology pool" to manage** — we piggyback on the topology diversity that regression already requires (IPI, DR, External, Provider-Client, etc.).
4. **Simpler orchestration** — one pipeline extension (a post-regression stage) instead of a parallel verification pipeline.

### Topology Categories

Fixes are grouped by the topology they require. The regression pipeline already provisions clusters for these topologies:

| Topology | Description | Typical Fix % |
|----------|-------------|---------------|
| Standard IPI | Single ODF cluster (AWS/vSphere/etc.) | 60-70% |
| Regional DR | Multi-cluster DR pair | 5-10% |
| Metro DR | Stretched cluster | 5-10% |
| Provider-Client | Managed service pair | 5-10% |
| External Mode | External Ceph + ODF | 5-10% |
| LSO/Baremetal | LSO-specific setups | 5% |

### Expected Impact

For a typical Lane C with 20 fixes: **0 additional clusters** for verification (down from 20+). The only clusters provisioned are the ones regression already needs.

---

## Pillar 2: AI-Driven Test Selection — "Test What Matters"

### Current State

No systematic mapping from fixes to required regression tests. The full regression suite runs for every Lane C release regardless of what changed.

### Proposed Model

For each Lane C release, automatically determine the minimal regression test set based on the actual fixes included.

### How It Works

1. **Input**: List of Jira/Bugzilla fixes in the release (bug IDs, affected components, changed files from upstream repos: noobaa-core, noobaa-operator, rook, ocs-operator, ramen, etc.)
2. **Analysis**: AI-assisted mapping from each fix to:
   - Tests that directly exercise the fixed code path
   - Tests for adjacent functionality that could regress
   - Confidence score for each test's relevance
3. **Output**: A curated test manifest (pytest markers or test list file) scoped to this specific release
4. **Guardrails**: Always include a baseline "smoke" suite (core functionality) regardless of fixes, to catch unexpected regressions

### Implementation Phases

| Phase | Approach | Details |
|-------|----------|---------|
| Phase 1 (near-term) | Manual with AI assistance | Engineer feeds fix descriptions to AI, gets test suggestions, curates manually |
| Phase 2 (medium-term) | Semi-automated | Tool reads Jira fix list, cross-references with code changes and test coverage data, outputs draft test manifest for human review |
| Phase 3 (long-term) | Fully automated | Integrated into the z-stream pipeline, test manifest generated and executed with minimal human review |

### Technical Implementation in ocs-ci

- Add `z_stream` marker to `pytest.ini` alongside existing tier and squad markers
- Create marker definitions in `ocs_ci/framework/pytest_customization/marks.py`
- Build fix-to-test mapping using component metadata and code path analysis
- Integrate with existing test selection mechanisms in `ocs_ci/framework/pytest_customization/ocscilib.py`

### Expected Impact

Reduce regression test execution from full suite to **30-50% of tests** per release, cutting both cluster time and human review time proportionally.

---

## Pillar 3: AI-Powered Fix Verification Agent — "Hybrid Agent"

### Current State

Each fix is manually verified by an engineer who reproduces the bug scenario, confirms the fix, and runs surrounding regression tests. This takes 2-4 engineer-hours per fix and doesn't produce reusable test coverage.

### Proposed Model

An AI agent that operates in two phases — fast direct verification on the live regression cluster, followed by automated ocs-ci test generation for permanent coverage.

### Phase A: Direct Verification (fast, immediate)

The AI agent connects to the live ODF cluster (the same one that just ran regression) and directly verifies each fix:

1. **Input**: The agent receives the Jira/Bugzilla fix details — bug description, reproduction steps, affected component, and the code diff from upstream repos (noobaa-core, noobaa-operator, rook, ocs-operator, ramen, etc.)
2. **Scenario execution**: The agent translates the reproduction steps into live cluster operations:
   - Creates required resources (PVCs, pods, buckets, etc.) via `oc` / Kubernetes API
   - Reproduces the original failure scenario to confirm the bug existed in the previous build
   - Validates the fix resolves the issue
   - Runs basic regression checks on adjacent functionality
   - Cleans up created resources
3. **Output**: Pass/fail report with evidence (command outputs, resource states, logs)

**Execution parameters:**
- **Timeout**: 15 minutes per attempt per fix
- **Retries**: Up to 3 attempts on failure
- **Worst-case per fix**: 45 minutes (3 x 15 min)

### Phase B: Test Generation (durable, reusable)

After successful direct verification, the agent generates an ocs-ci pytest:

1. **Input**: The verification scenario from Phase A + ocs-ci codebase patterns
2. **Test generation**: The agent writes a pytest test that:
   - Uses ocs-ci fixtures and factories (`pvc_factory`, `pod_factory`, etc.)
   - Follows existing test patterns in the relevant test directory
   - Inherits from appropriate base class (`ManageTest`, `E2ETest`, `EcosystemTest`)
   - Includes proper markers (squad, tier, `polarion_id`, z-stream marker)
   - Has cleanup via finalizers
3. **PR creation**: Opens a PR against the relevant release branches (e.g., `release-4.16`, `release-4.17`, etc.), backporting the test across versions
4. **Human review**: Engineer reviews the generated test for correctness before merge

**Not every fix needs Phase B** — the decision is based on:
- Whether existing tests already cover the code path (if yes, Phase A only)
- Fix severity and customer impact (high severity = always generate a test)
- Component risk profile

### Pipeline Integration

The AI agent runs as a **separate Jenkins job** in ocs4-jenkins, called from the regression job:

```
ocs4-jenkins regression job
    │
    ├─ Deploy cluster
    │   └─ (if deploy fails → teardown, NO verification)
    │
    ├─ Run regression tests
    │
    ├─ Post-test health check
    │   └─ (if cluster unhealthy → teardown, NO verification)
    │
    ├─ Trigger AI Agent Verification job ──────────────┐
    │   (separate Jenkins job)                         │
    │                                                  │
    │   Receives: kubeconfig, cluster details,         │
    │             fix list for this topology            │
    │                                                  │
    │   For each fix:                                  │
    │    ├─ Attempt verification (15 min timeout)      │
    │    ├─ Retry up to 3 times on failure             │
    │    └─ Report pass/fail/skipped                   │
    │                                                  │
    │   Returns: consolidated report ──────────────────┘
    │
    └─ Teardown cluster
```

**Benefits of a separate job:**
- Can retry verification without re-running regression
- Can be triggered manually for debugging or re-verification
- Doesn't block regression pipeline teardown reporting
- Can evolve independently from the regression pipeline

**Gate condition:** The AI agent verification job is only triggered if:
1. Cluster deployment succeeded
2. Regression tests completed (pass or fail — tests may fail but cluster is still usable)
3. Post-regression cluster health check passes

### Agent Architecture

```
┌──────────────────────────────────────────────────────┐
│                Fix Verification Agent                 │
├────────────────────────┬─────────────────────────────┤
│  Phase A: Direct       │  Phase B: Test Generation   │
│  Verification          │                             │
│                        │                             │
│  • Read fix details    │  • Analyze ocs-ci patterns  │
│    from Jira/BZ        │  • Generate pytest test     │
│  • Read code diffs     │  • Add markers, fixtures,   │
│    from upstream repos │    finalizers               │
│  • Connect to live     │  • Open PR to release       │
│    ODF cluster         │    branches                 │
│  • Reproduce bug       │  • Backport across          │
│  • Verify fix          │    versions                 │
│  • Generate report     │                             │
└────────────────────────┴─────────────────────────────┘
         ↓                          ↓
   Pass/Fail Report          PR with new test
   per fix                   (human review required)
```

### What the Agent Needs Access To

| Resource | Purpose |
|----------|---------|
| Jira/Bugzilla API | Bug description, reproduction steps, affected component |
| Upstream repos (noobaa-core, noobaa-operator, rook, ocs-operator, ramen, etc.) | Code diffs for each fix |
| Cluster kubeconfig (from regression job) | Live cluster access for verification |
| `oc` CLI / Kubernetes API | Resource creation and verification |
| ODF tooling (ceph CLI, noobaa CLI, etc.) | Component-specific verification |
| ocs-ci repo | Test patterns, fixtures, markers for test generation |

### Expected Impact

- **Phase A**: Reduces per-fix verification from ~2-4 engineer-hours to ~15-30 min of agent execution + brief human review of the report
- **Phase B**: Builds up regression coverage automatically, reducing future Lane C regression gaps (+20-30 new tests per Lane C cycle)
- Combined with cluster reuse (Pillar 1), verification adds zero infrastructure cost

---

## Pillar 4: Multi-Version Efficiency — "Verify Once, Qualify Many"

### Current State

Same or similar verification work is repeated across 5-6 Term 1 versions.

### Proposed Model

Identify which fixes and tests are version-independent and consolidate verification.

### How It Works

1. **Fix classification** — For each fix in Lane C, determine:
   - Is the fix identical across versions (same patch, same code path)?
   - Or does it differ per version (version-specific behavior, different Ceph versions)?

2. **Tiered verification**:
   | Fix Type | Verification Approach |
   |----------|----------------------|
   | Identical across versions (50-70% of fixes) | Full verification on latest version only. Reduced smoke test on older versions. |
   | Version-specific | Full verification on each affected version. |

3. **Anchor versions**: Run the full regression suite on 1-2 "anchor" versions (e.g., latest Term 1 + oldest Term 1). Run only targeted tests on intermediate versions.

### Expected Impact

For identical fixes (50-70% of Lane C): reduce per-version effort by ~60-70%. Total effort across all versions could drop by **40-50%**.

### Risks and Mitigations

- **Version-specific regressions missed**: A fix may behave differently on older OCP/Ceph combinations → Mitigate with smoke test baseline on all versions
- **Process discipline**: Requires consistent fix classification → Build this into the release checklist

---

## Fix-to-Topology Mapping

A critical enabler for Pillars 1 and 3: knowing which fixes need which topology.

### Approach: Semi-Automated → Metadata-Driven

| Phase | Method | Details |
|-------|--------|---------|
| Initial | Semi-automated | AI reads fix descriptions from Jira and suggests the required topology. Engineer confirms or overrides. |
| Target | Metadata-driven | Jira component/label fields map directly to topology categories. No manual step needed. |

### Topology Assignment Rules (initial heuristic)

| Jira Component / Keywords | Topology |
|---------------------------|----------|
| RBD, CephFS, PVC, snapshot, clone, CSI | Standard IPI |
| Regional DR, RDR, ramen, failover, relocate | Regional DR |
| Metro DR, MDR, stretch, arbiter | Metro DR |
| Provider, consumer, managed service, ROSA | Provider-Client |
| External Ceph, external mode, RHCS | External Mode |
| LSO, local storage, baremetal | LSO/Baremetal |
| MCG, NooBaa, bucket, S3, OBC | Standard IPI (usually) |
| OCS Operator, upgrade, deployment | Standard IPI |

---

## End-to-End Pipeline Flow

```
Lane C Release Planned
        │
        ▼
┌─────────────────────────────────┐
│  1. Fix Intake & Classification │
│                                 │
│  • Pull fix list from Jira/BZ   │
│  • Classify each fix by         │
│    required topology            │
│    (semi-auto → metadata)       │
│  • Classify fix type:           │
│    identical vs version-specific│
└───────────────┬─────────────────┘
                ▼
┌─────────────────────────────────┐
│  2. AI Test Selection           │
│     (Pillar 2)                  │
│                                 │
│  • Map fixes → relevant tests   │
│  • Generate scoped test         │
│    manifest per topology        │
│  • Include smoke baseline       │
└───────────────┬─────────────────┘
                ▼
┌─────────────────────────────────┐
│  3. Regression Execution        │
│     (ocs4-jenkins)              │
│                                 │
│  • Provision clusters per       │
│    topology (already happens)   │
│  • Run scoped regression suite  │
│  • Post-test health check       │
│  • DO NOT tear down cluster     │
└───────────────┬─────────────────┘
                ▼
┌─────────────────────────────────┐
│  4. AI Agent Fix Verification   │
│     (Pillar 3 — separate job)   │
│                                 │
│  Gate: deployment succeeded AND │
│        cluster is healthy       │
│                                 │
│  For each fix on this topology: │
│   • Phase A: Direct verify      │
│     (15 min timeout, 3 retries) │
│   • Phase B: Generate test      │
│     (if needed)                 │
│                                 │
│  Output: consolidated report    │
└───────────────┬─────────────────┘
                ▼
┌─────────────────────────────────┐
│  5. Multi-Version Handling      │
│     (Pillar 4)                  │
│                                 │
│  • Identical fixes: full verify │
│    on latest, smoke on others   │
│  • Version-specific: full       │
│    verify on each version       │
└───────────────┬─────────────────┘
                ▼
┌─────────────────────────────────┐
│  6. Teardown & Reporting        │
│                                 │
│  • Tear down all clusters       │
│  • Consolidated Lane C report:  │
│    regression results +         │
│    per-fix verification status  │
│  • PRs for generated tests      │
└─────────────────────────────────┘
```

---

## Implementation Roadmap

| Phase | Focus | Status | Key Actions | Dependencies |
|-------|-------|--------|-------------|--------------|
| **1** | Fix-to-topology mapping | **DONE** | Parses DFBUGS template for platform/deployment type; AI classifies to topology; `--plan-deploy` prints Jenkins API calls | Jira API access |
| **2** | Z-stream markers & test selection | **DONE** | Per-component markers (`zstream_X_Y_Z_mcg`, etc.) + global marker; per-version test index; AI-driven test selection; per-deployment `TEST_MARK_EXPRESSION`; PR with multi-mark commits + DCO + label | Phase 1 |
| **3** | Cluster reuse pipeline | **PARTIAL** | `--deploy` triggers Jenkins per topology with `RUN_TEARDOWN=false`; per-deployment test filtering via component markers; platform priority (vsphere > ibmcloud > aws > ...); agnostic bugs join existing deployments; Jenkins API tools built; actual ocs4-jenkins job changes not yet done | ocs4-jenkins access |
| **4** | AI verification agent (Phase A) | Not started | Build agent that takes kubeconfig + fix details and runs direct verification | Phase 3 (live cluster) |
| **5** | AI test generation (Phase B) | Not started | Build agent capability to generate ocs-ci pytest from verification scenario | Phase 4 (working agent) |
| **6** | Multi-version optimization | **Foundation** | Per-version test indexes built (4.10-4.21); fix classification logic not yet built | Phase 1 |
| **7** | Metadata-driven mapping | Not started | Add topology fields to Jira workflow; replace text parsing with metadata lookup | Jira workflow changes |
| **8** | Full pipeline integration | **In progress** | Inspect → test selection → PR → topology → Jenkins trigger working; analysis + verification not yet connected | All phases |

---

## Success Metrics

| Metric | Current (estimated) | Target |
|--------|-------------------|--------|
| Additional clusters for fix verification | ~15-25 per release per version | **0** (reuse regression clusters) |
| Regression test scope (% of full suite) | ~100% | **30-50%** |
| Engineer time per fix verification | ~2-4 hours | **~30 min** (review agent report) |
| Per-version effort for identical fixes | Full verification | **Smoke only (~20%)** |
| Total engineer-hours per Lane C cycle | Baseline TBD | **60-70% reduction** |
| Infrastructure cost per Lane C cycle | Baseline TBD | **60% reduction** |
| Automated tests from AI generation | 0 | **+20-30 new tests per cycle** |

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| AI agent misinterprets reproduction steps | False pass/fail on verification | Human reviews all Phase A reports; start with simpler fixes and expand scope gradually |
| Generated tests are superficial | Low-value test coverage | Engineer review required before merge; use existing ocs-ci tests as few-shot examples |
| Regression leaves cluster in degraded state | Verification blocked | Health check gate before triggering verification; if unhealthy, skip verification and tear down |
| Test interference between fix verifications | False failures | Order verifications from least to most destructive; clean up resources between fixes |
| Agent timeout/hang | Blocks cluster teardown, wastes infra | Hard 15 min timeout per attempt, 3 retries max, forced teardown after all attempts |
| Version-specific regressions missed with tiered verification | Escaped defect | Smoke test baseline runs on all versions; anchor version strategy covers oldest + newest |
| Fix-to-topology misclassification | Fix verified on wrong topology | Semi-automated mapping with engineer confirmation; transition to metadata-driven over time |

---

## Appendix: Relevant Repositories

| Repository | Purpose |
|------------|---------|
| `ocs-ci` (this repo) | Test framework — z-stream markers, test selection, generated tests land here |
| `ocs4-jenkins` (gitlab.cee.redhat.com/ocs/ocs4-jenkins) | CI/CD pipelines — regression jobs, verification job trigger, cluster lifecycle |
| `noobaa-core` | Upstream NooBaa core — fix diffs for MCG/S3/bucket fixes |
| `noobaa-operator` | Upstream NooBaa operator — fix diffs for MCG operator fixes |
| `rook` | Upstream Rook — fix diffs for Ceph CSI, RBD, CephFS fixes |
| `ocs-operator` | OCS/ODF operator — fix diffs for operator-level fixes |
| `ramen` | DR orchestrator — fix diffs for Regional DR / Metro DR fixes |
