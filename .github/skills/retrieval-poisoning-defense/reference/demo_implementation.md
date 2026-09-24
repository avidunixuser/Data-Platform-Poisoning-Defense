# Demonstration implementation and Azure deployment

**Implementation specification; reviewed 2026-09-24.** The repository already has
runnable detection scenarios. It does not yet contain the UI, API, operational
write gate, live agent/MCP server, containers, or deployment assets described
here. Adding this guide does not implement or deploy those components.

Build an **offline-first Poisoning Defense Lab** with a browser UI and a headless
client using the same backend. Add explicit live Foundry integration after the
local demonstration works. Keep the [enforcement contract](enforcement_and_scaling.md)
and [Foundry/private-network design](foundry_implementation.md) as the authority
for application boundaries. A convincing demonstration is not production
certification, and a detector flag is not proof of poisoning.

## 1. Establish what is runnable and what must be built

| Capability | Present today | Demonstration implementation needed |
| --- | --- | --- |
| Numeric, regex, and Unicode checks | Python detector APIs | Shared scenario runner and evidence presentation |
| Offline end-to-end example | `scripts\smoke_test.py`, including synthetic data and file-backed SQLite | Reusable interactive scenario selection and retained operation history |
| Semantic text screening | Configurable Foundry embedding provider | Explicit opt-in live configuration and per-layer status in the UI |
| Read-only source loading | SQL/PostgreSQL and Kusto loaders | Approved source catalog and bounded input selection |
| Write enforcement | Design guidance only | Intake, durable staging, operation ledger, workers, trusted publisher, and status API |
| Agent, Model Router, MCP, A2A | Integration guidance only | Configured agents, authenticated tool adapters, and actual request traces |
| Durable memory for every AI agent | No custom agent-memory adapter yet | Scoped Cosmos NoSQL memory API, gated memory publication, retention, and cross-agent isolation |
| Azure-hosted demonstration | Deployment design guidance only | Application containers, IaC, environment configuration, and rollout automation |

The existing offline example can be run now from the repository root, after the
core environment is installed as described in README:

```powershell
.\.venv\Scripts\python.exe .github\skills\retrieval-poisoning-defense\scripts\smoke_test.py
```

It makes no Azure calls and uses temporary data that is cleaned up after the
run. It does not start a web application, demonstrate a live semantic model, or
leave a durable operation history for a presenter to browse.

## 2. Build a reusable scenario backend

Use **Streamlit** for the presentation UI, **FastAPI** for the application API,
and separate bounded **Python workers** for evaluation and publication. This
reuses the existing Python ecosystem without making the UI responsible for
statistical calculations or write decisions.

```text
Browser UI / headless client / optional MCP adapter
    -> authenticated API
    -> durable staging + operation ledger + dispatch
    -> validation worker -> protected decision
    -> trusted publisher -> isolated demonstration target
    -> status/findings API -> UI, CLI, and optional investigator
```

Suggested repository layout, **to be implemented**, not existing commands or
files:

```text
demo\
  backend\     API, scenario runner, contracts, persistence adapters
               and scoped agent-memory read/proposal adapters
  worker\      evaluation, dispatch/retry handling, publication
  ui\          Streamlit screens; calls the API, not datastore writers
  cli\         headless client using the same API and result contract
  scenarios\   approved synthetic fixtures and versioned evaluation profiles
  tests\       API, lifecycle, restart, permission, and presentation cases
  containers\  reviewed Dockerfiles and local development configuration
  requirements.txt
azure.yaml     optional azd service mapping, once authored
infra\         approved Bicep or Terraform deployment assets
```

Keep demo dependencies separate from the skill's core requirements. Do not
duplicate or fork detector algorithms into UI handlers. Import the bundled
modules through a stable package/import path and include the same code version
in every application image. Reuse the existing
`smoke_test.majority_cluster_fixture` for the isolated-cluster scenario, or
extract a shared fixture in a later code change rather than copy its math.

Define one result contract containing operation/run/scenario IDs, execution
mode, enforcement mode, source IDs and row positions, dataset/reference/model/
policy versions, thresholds, measured signals, required-check coverage, and
timestamps. Store the actual detector outputs, not UI-generated approximations.
Keep write outcome, detector outcome, and optional agent explanation separate:
an explanation failure must not rewrite the detector evidence or claim a commit.

Use synthetic fixtures by default. Keep evaluator-only ground truth, such as
poisoned-row membership and corrected labels, out of detector/agent inputs;
label-flip and spectral checks still receive the batch's observed labels.
Raw document previews are allowed only for approved synthetic data, rendered as
escaped text rather than executable markup. Do not expose arbitrary file paths,
SQL, connection strings, or unrestricted uploads through a demonstration form.

## 3. Implement the presentation layer

| Screen | Controls and evidence |
| --- | --- |
| Scenario selection | Versioned scenario/profile, source role, execution mode, and one of the three enforcement modes |
| Detection results | Source IDs, numeric scores and thresholds, reason codes, coverage gaps, and reference versions; distinguish no flags from a guarantee of safety |
| Visual evidence | Distribution plots, spectral scores, and optional two-dimensional embedding projections; explain that projections do not determine the full-dimensional verdict |
| Write lifecycle | Pending/running/held/rejected/approved/committing/committed/reconciling history, actual published records, and separate acknowledgement/commit/visibility timings |
| Investigation | Optional live agent explanation, authorized tool trace, serving model when returned, and links to the recorded evidence |
| Agent memory | Current authorized agent/session scope, approved memory versions, pending proposals, source references, and expiry; no global all-agent history view |

Require a persistent execution-mode badge on every screen and export:

- **Offline:** real local detector computations; cloud-only checks explicitly
  unavailable unless a separately labeled synthetic provider is selected.
- **Replay:** an identified, sanitized saved run, with its capture time and
  configuration. Never present replay as a new live inference.
- **Live Azure:** actual configured SDK/service calls. An error stays visible;
  do not silently substitute offline or replay results.

Keep execution mode separate from `post_write_audit`, `inline_gate`, and
`async_gate`. A mode switch must create a new run/profile, not mutate accepted
operations. The server, not a browser field, determines which mode and check
policy a caller is allowed to select. Only an authorized presenter can change
demo thresholds; record the change and rerun, leaving old results intact.

Submit work once with an idempotency key, then poll by operation ID. A Streamlit
rerun or browser reconnect must not enqueue the operation again. Store lifecycle
state in the backend, not only in session state. Keep credentials out of browser
code and UI logs. In local development, bind to loopback and use synthetic data;
remote access requires authentication and authorization before exposure.

## 4. Implement the write gate and observable API

The following are **proposed** domain endpoints. They do not exist in this repo
yet. UI, headless client, and MCP must share their backend checks.

| Operation | Contract |
| --- | --- |
| `GET /scenarios` | Return only approved profiles and required detector layers available to this caller |
| `POST /operations` | Validate and authorize a candidate/batch; bind mode, policy, target, and idempotency key; dispatch under the selected contract |
| `GET /operations/{id}` | Return an authorized status view with write/evaluation states, timestamps, versions, and sanitized errors |
| `GET /operations/{id}/findings` | Return bounded evidence and candidate source IDs, without exposing another tenant's data |
| `GET /published-records` | Read the actual isolated target/projection, not a UI-maintained counter claiming that a write occurred |
| `POST /operations/{id}/review-requests` | Request review, not automatically approve or publish; reviewer decisions need a separately authorized workflow |

For asynchronous intake, return `202 Accepted`, an opaque operation ID,
`Location`, and `Retry-After` only after durable staging/admission and recoverable
dispatch intent exist. A status endpoint's `200 OK` means the status was read,
not that the write succeeded. For inline mode, wait for required checks and a
known write outcome; for post-write auditing, report a commit first and later
evaluation independently. Do not label an already committed audit failure as
if no write occurred.

### Local implementation

Use a file-backed SQLite operation/job ledger on a persistent local disk and
a separate staging location and demonstration target. A worker process can
claim durable jobs transactionally, with an owner/lease and conditional state
updates. Start with bounded concurrency; do not use a single shared connection
across arbitrary threads or put the database on a network share.

An in-memory queue, FastAPI background task, or fire-and-forget thread alone is
not durable execution. Persist work before acknowledging it; recover abandoned
leases after restart and distinguish retryable failures from terminal ones.
Do not reuse the smoke runner's temporary directory as durable application state.

SQLite files/processes under the same local user illustrate lifecycle behavior,
not a non-bypassable security boundary. Label the prototype accordingly. The
cloud implementation must enforce distinct service identities and protected
stores, as described in the Foundry guide.

### Publication correctness

Record the exact candidate hash, target/base version, and evaluation context.
The publisher must read an authentic protected decision, not trust an `approved`
flag in a request or queue message. Apply an idempotent or conditional target
write, confirm the result, and persist its receipt. A target commit and ledger/
broker acknowledgement may not be atomic; reconcile unknown outcomes before
retrying. A repeated idempotency key with a different payload is a conflict.

Keep unapproved staging outside all serving/training views. Do not provide
proposing clients or agents with a second direct-write route. Invalid inputs,
missing mandatory checks, and model timeouts hold/reject the candidate rather
than fail open. All content layers required by the chosen profile must run;
a negative regex result does not excuse missing semantic evaluation.

Cohort-dependent label/spectral/drift checks need an appropriate bounded batch.
Do not evaluate an arbitrary single row and call it protected by all detectors,
partition k-NN by label, or publish after skipping undersized classes.
Preserve safe per-entity ordering and the reference context when scaling.

## 5. Supply deterministic scenarios and a presenter walkthrough

| Scenario | What to demonstrate accurately |
| --- | --- |
| Trusted control data | Applicable checks run without flags; publication follows the configured policy, not a blanket "safe" verdict |
| Embedding magnitude/category outlier | Real Mahalanobis/cosine signals and source-ID association using compatible synthetic references |
| Known embedded instruction | Regex/Unicode findings; live semantic evidence only when that layer actually ran |
| Scattered minority label flip | Neighborhood disagreement surfaces a candidate, not an automatically corrected label |
| Isolated consistently mislabeled cluster | Existing fixture: k-NN catches 0/40 affected rows; spectral review includes all 40 among 42 candidates |
| Legitimate feature drift or benign security documentation | A flag can require review without establishing malicious intent |
| Missing baseline or unavailable semantic service | Incomplete coverage is explicit; a gated profile requiring that check does not publish |

The fixture-specific 40/40 result is not a general recall guarantee. Low-dimensional
synthetic vectors are not real text embeddings; label the representation and do
not mix them with live embeddings or a reference from another model.

A short presenter sequence:

1. Run a clean control and inspect its real detector evidence and published record.
2. Submit a known suspicious scenario in post-write mode; show the commit-to-flag
   exposure interval.
3. Submit an independent copy in inline mode; show that it never becomes published.
4. Submit it in async mode; show prompt acknowledgement, later held status, and
   unchanged published data. Show restart/duplicate-submission recovery.
5. Run the isolated-cluster comparison and explain why local and global checks
   differ, including the two extra review candidates.
6. If enabled, ask the live investigator to explain the evidence, then contrast a
   simple request with a complex one through Model Router. Display observed model
   selection, not a promise that either prompt always selects a particular model.

Offer the same scenarios through the headless API client. Save a sanitized,
versioned replay bundle as a deliberately selected presentation backup. Do not
fabricate a green service indicator or pre-recorded result after a live failure.

## 6. Add live Foundry, MCP, and optional A2A integration

Keep the existing configurable semantic embedding provider. Server-side
`FOUNDRY_EMBEDDINGS_ENDPOINT` and `FOUNDRY_EMBEDDINGS_MODEL` must identify an
approved endpoint/deployment; reuse one classifier per worker to cache its seeds.
Record embedding model/version and preserve compatible reference/calibration
data. Offline mode must not accidentally initialize a network provider.

Configure the investigation agent with the trusted skill instructions and
bounded application tools. A document in a retrieval index is not a substitute
for loading trusted agent instructions. The agent explains evidence; it does
not recompute statistics from charts or grant itself publication rights.

For Model Router, follow the [model-routing section](foundry_implementation.md#model-routing-for-agent-requests).
Configure the agent's chat deployment separately from embedding configuration.
Show the actual returned serving model, usage, and measured latency; show
unavailable metadata as unavailable. Restrict model pools, inference residency,
and tool capabilities. Router failures must not weaken required write checks.
This does not reroute Copilot's own model just because it loaded this skill.

Implement a remote MCP adapter exposing bounded equivalents of submission,
audit, status, findings, and review-request operations. It calls the same domain
service as the REST API; it is not a privileged bypass. Use approved HTTPS
transport, caller authentication, tool allowlists, schema validation, and
per-operation authorization. Large datasets stay in protected storage; use
opaque references and bounded results rather than filling the agent context.

Foundry cannot call an arbitrary localhost MCP process in the presenter's
machine. Deploy a reachable authenticated endpoint in the approved topology.
For the private profile, retain the documented direct private ACA MCP path until
any APIM-generated MCP route has passed real agent-originated acceptance. Do
not open a public tunnel around the intended network controls for convenience.

Add A2A only if cross-agent investigation is part of the presentation. Start
with one specialist, exchange case references/minimized evidence, and verify the
selected protocol, identity, and private route. Task completion is not a commit
receipt. A2A is unnecessary for scaling numerical detection workers.

### Add Cosmos memory to every participating agent

Implement the [Cosmos agent-memory contract](foundry_implementation.md#cosmos-db-memory-for-every-ai-agent)
for proposing, coordinating, investigating, and approved A2A specialist agents.
One Cosmos service family can support them all, but each agent/user/tenant
scope is authorized separately. Keep custom memory separate from platform-owned
Foundry state and protected gate decisions/receipts.

Add bounded recall/proposal/status operations to the backend and MCP adapter.
Agents recall only approved, scoped, unexpired versions; new memory proposals
are staged and screened before a trusted memory publisher writes the serving
store. No agent or browser gets a general Cosmos writer or a caller-selected
tenant/container connection. Optional shared-case memories need explicit grants.

For the local offline prototype, a namespaced SQLite adapter can exercise the
contract, but label it **local memory simulation**, not live Cosmos persistence
or cloud isolation. Live Azure mode uses the Cosmos adapter through private
connectivity and managed identity. Never silently switch persistence providers
after a Cosmos failure.

Demonstrate agent/session restart and scoped recall, then deny an unauthorized
second agent or tenant. Show pending memory excluded from recall, a reviewed
shared-case grant, expiry/revocation, and an ETag conflict. Keep memory unavailable
visible on errors. Chat-model routing can vary summary-generation models, but
memory embeddings and authorization policies must remain compatible and fixed.

Microsoft Prompt Shields remains a separate optional integration; this repo
does not call it. Do not label the existing seed-similarity classifier as a native
Prompt Shields verdict or a trained malicious-intent classifier.

## 7. Prepare deployment assets before using Azure deployment commands

**Nothing in sections 2-6 is a ready-to-run deployment today.** First implement
the application and author the following assets in a separately reviewed change:

- Container images with real entrypoints for UI, API/MCP, memory API, validator,
  and separately authorized business/memory publishers.
  The repository-root build context must include the required skill modules;
  review ignore rules so hidden `.github` assets are not accidentally excluded.
- A separate demo dependency manifest/lock strategy and an environment template
  containing only nonsecret names/defaults. Keep local state, environment secrets,
  and generated evidence out of source control and container layers.
- Bicep or Terraform under `infra`, selected under the organization's standards,
  with explicit service identities, data scopes, network mode, region, budgets,
  outputs, and initial admission-disabled configuration.
- Cosmos memory containers, indexing/TTL policies, agent-principal registry,
  memory-specific identity grants, and migration/revocation/backup procedures.
  Keep candidate/serving memory and gate authority in different permission scopes.
- If using Azure Developer CLI, an `azure.yaml` mapping the implemented services
  to actual resources and build paths. Use documented container/job deployment
  support; a source folder alone is not an azd template.
- Versioned synthetic seed/reference loading and explicit environment-scoped
  reset/retention operations. Neither startup nor deployment may clear existing
  accepted operations or overwrite a trusted reference automatically.

Provide startup, readiness, and liveness behavior. Readiness must reflect whether
the service can do its declared job; do not mark a live-required profile ready
without its reference/model configuration. Keep liveness focused on process
health rather than causing restart storms during a shared dependency outage.
Support graceful shutdown and bounded worker lease renewal.

## 8. Deploy the implemented demonstration to Azure

These are **conditional runbook steps**, to execute only after the preceding
assets exist and the specific environment/resource changes are approved.

### Choose the profile and inventory

For an enterprise demonstration, use the existing
[private-network and identity guide](foundry_implementation.md).
The UI and memory services are additional workloads within the existing compute
family; all AI agents use Cosmos NoSQL application memory, not a new storage
service family.
MCP and A2A are interfaces, not additional cloud service families.
Secured Standard Foundry brings its required Search/Storage/Cosmos dependencies;
do not omit them merely because the demo's business target is different.

The selected deployment profile keeps the UI and data/tool paths private and
requires authenticated operator access through the approved network. A public-UI
showcase would be a different, separately approved design, not an alternative
enabled by this runbook. Never switch to public access as a recovery shortcut.

### Map local state to durable Azure services

| Local demonstration component | Azure implementation |
| --- | --- |
| Streamlit UI | Container App, authenticated HTTPS access; session/reconnect handling does not own durable job state |
| FastAPI intake/status and optional MCP facade | Separately permissioned Container App; governed API entry point where selected |
| Python evaluator and publisher | Separate ingress-disabled Container Apps/workloads with bounded queue consumers and distinct identities |
| Pending candidates, references, evidence | Separate Blob containers/accounts and narrowly scoped data roles |
| SQLite operation, decision, and receipt records | Separately permissioned Cosmos NoSQL containers or an approved transactional SQL design |
| Local agent-memory simulation | Cosmos NoSQL session/long-term and explicitly shared-case memory, separate from candidates, gate records, and Foundry-managed backing state |
| Local job claims | Service Bus work queues with reliable dispatch/outbox, retries, leases, and dead-letter handling |
| Local demonstration target | A dedicated synthetic-data target with its own publication/read boundary, not a production database |
| Console logs | Structured Azure Monitor/Application Insights telemetry without payload/credential leakage |

Do not carry the SQLite file into a Container App's ephemeral filesystem and
claim durable acceptance. Do not turn a shared file mount into an assumed
distributed transaction/authorization mechanism. Migrate the persistence and
dispatch adapters deliberately while preserving the API/lifecycle contract.

### Private network and managed identity for each service

The default Azure profile is private and keyless. Use the exact subnet,
endpoint/DNS, SKU, and role details in the
[Foundry implementation guide](foundry_implementation.md); the following is
the per-service deployment checklist, not deployed configuration.

Attach managed identities to the **callers that execute operations**. A storage
service's own identity is not required merely because clients authenticate with
managed identity; it needs an outbound identity only for features that require
one, such as customer-managed keys. Do not attach the publisher identity to every
service or assign Owner/Contributor universally.

| Service/workload | Private-network requirement | Identity and least-privilege requirement |
| --- | --- | --- |
| Streamlit UI | Internal ACA environment and approved private operator/client path; HTTPS and correct ingress scope | Entra user sign-in; distinct UI service identity for authorized API calls, not Cosmos access or implicit delegation of every user |
| Intake/status/MCP API | Private ACA route or tested private APIM route; no alternate public backend | Explicit runtime UAMI; staging/admission and queue-send permissions only; validate custom API audience, roles, tenant, and record ownership |
| Agent-memory API | Private authenticated endpoint, reachable only by approved agents/apps | Separate memory-reader/proposal trust scopes; reader can query only assigned serving containers; proposals use the gate, not serving-memory writes |
| Validation workers | Ingress disabled; private routes to staging, queues, ledgers, and embeddings | Per-workload UAMI; assigned queue receive/send, reference/staging read, decision write, and inference access; no target writes |
| Business and memory publishers | Ingress disabled; target/memory data plane reachable privately | Distinct scoped UAMIs; read authentic decisions and write only their assigned targets/receipts; neither creates its own approval |
| Foundry agents and models | Standard BYO-VNet profile, account PE/DNS, private dependencies and tool routes; private access does not prove inference residency | Actual agent identity or explicitly selected project MI; custom memory/proposal app roles; supported inference permissions for the chosen endpoint; no agent direct-write bypass |
| Cosmos memory and operation stores | `Sql` PE and `privatelink.documents.azure.com`, account public access disabled, correct canonical hostnames | Native Data Reader/Contributor or narrower reviewed data actions at assigned containers; local key auth disabled after migration; memory/decision/admission scopes separated |
| Blob Storage | Required storage-service PEs and linked DNS; staging/reference/evidence scopes isolated; public data access disabled | Container-scoped data roles for runtime MIs; no keys/SAS in browsers or agent context; reference artifacts runtime-read-only |
| Service Bus | Premium namespace private endpoint and `privatelink.servicebus.windows.net`; public access disabled | Per-queue Data Sender/Receiver for producers, consumers, and scaler as needed; no shared namespace-admin credentials |
| Container Registry | Premium Private Link; both registry and regional data endpoints resolve privately; private build and pull paths | Pull-only MI with `AcrPull` or repository-scoped ABAC reader as appropriate; separate build identity for push; registry admin credentials disabled |
| API Management, if selected | Supported Standard v2 inbound gateway PE plus outbound integration; separate backend DNS/routing; public gateway disabled | Dedicated backend MI/custom API role; preserve original caller authorization and do not treat an APIM token as every end user's authority |
| Foundry backing AI Search | `searchService` PE and `privatelink.search.windows.net`; public data access disabled; indexer outbound links handled separately | Platform-required roles only on backing resources; any application caller gets scoped data roles, not shared admin keys |
| Azure Monitor | AMPLS/DNS and per-resource public ingestion/query controls where private telemetry is required | Emitter/collector identity with supported ingestion permissions; verify each telemetry path, not a presumed universal Monitor grant |
| Key Vault, if required | `vault` PE and `privatelink.vaultcore.azure.net`; public data access disabled | Narrow secret/key/certificate data roles only for the callers that need them; no runtime vault administrator |
| Entra and networking foundation | Preserve approved identity/control-plane egress; this design does not give Entra a private endpoint or make the tenant air-gapped | Separately privileged deployment/IAM identity manages roles, VNets, DNS, and PE approvals; runtime identities cannot reconfigure these boundaries |

Cover every resource instance, not just each product family. A distinct Cosmos
memory account or embedding account needs its own private route, DNS, roles, and
public/local-auth settings. Preserve TLS validation and do not infer public
denial from private endpoint existence. Optional business targets must pass the
Foundry guide's target-specific network/authentication acceptance as well.

### Provision foundations and publish application revisions

1. **Approve the environment.** Confirm subscription, tenant, region, resource
   groups, model quotas/providers, cost limits, data residency, operator/private
   CI access, and owners. Use a dedicated demonstration target and synthetic data.
2. **Review deployment changes.** Parameterize the chosen IaC, inspect a Bicep
   what-if or Terraform plan for that scope, and obtain change approval. Verify
   initial app admission is disabled and no shared production resource is reset.
3. **Provision network, identity, and data dependencies.** Apply the Foundry
   guide's selected VNet/endpoint/DNS design, registry, monitoring, staging,
   queue, and independently protected ledger scopes. Configure RBAC and custom
   API roles, including Foundry backing resources and each memory API/publisher.
   Provision isolated Cosmos memory containers, retention/index policies, private
   endpoints, and agent-scope mappings before enabling agents. Load the
   approved versioned synthetic reference artifacts before starting workers
   whose readiness depends on them; do not overwrite an existing baseline.
4. **Provision required live inference dependencies.** If the selected profile
   requires Azure embeddings or Model Router, deploy them and grant the intended
   runtime inference permissions before launching a revision that depends on
   them. Verify endpoint/configuration and keep chat routing separate from
   embedding model/reference compatibility.
5. **Build and publish versioned images.** Use an approved runner with actual
   private connectivity to ACR. Deploy immutable image versions/digests, not
   mutable `latest` tags. Runtime image pulls use their assigned identities;
   runtime identities do not get registry push or IAM administration.
6. **Deploy application workloads with admission closed.** Apply required
   environment references, identities, health probes, HTTPS, authentication,
   authorization, and bounded replica/worker concurrency. Configure access
   controls before making UI/API ingress remotely reachable, not afterward.
   Workers have no public ingress. Keep durable state outside the container and
   preserve old receipts during upgrades.
7. **Verify the browser and service hops.** Enforce Entra authentication for
   remote UI access and scenario/status/review permissions in the backend.
   Verify the UI's server-to-API identity/delegation deliberately: its managed
   identity does not automatically represent every end user. Do not trust
   spoofable identity headers. Verify WebSocket/reconnect and anti-forgery/origin
   behavior rather than disabling protections to make the UI work.
8. **Connect optional live agents and tools.** Create the agent, load trusted
   instructions, select its approved chat/router deployment, and connect the
   authenticated MCP and memory tools only after their endpoints are ready.
   Verify every registered agent's own scoped recall/proposal path and any
   explicitly authorized sharing. Add A2A only after its real route works.
   No browser receives model or Cosmos credentials.
9. **Exercise a canary.** Confirm reference versions and warm-worker readiness,
   then admit a controlled synthetic batch. Confirm outcomes in the real target
   and ledger, including denied writes and model/network failures. Complete the
   positive and negative checks in section 9 before general demonstration access.
10. **Enable admission and monitor.** Record the approved configuration/revision.
    Scale on HTTP and queue behavior within service/token/database limits. Keep
    an explicit rollback path that preserves accepted operations and schema
    compatibility; a UI traffic rollback alone does not undo queued work.

If the implemented project is made azd-compatible, the staged workflow can be:

```powershell
# Only after azure.yaml, application images, and reviewed IaC exist.
azd auth login
azd env new poisoning-defense-demo
# Configure and verify the approved subscription, region, and template inputs.
# Review the infrastructure change plan before the following provisioning step.
azd provision
azd package
azd deploy
```

This block is a future workflow, **not a command sequence that works against the
current repository**. Review each command's result before continuing. `azd up`
combines provisioning and deployment; it does not create the missing service
implementation or remove the need for approval. Private build/network constraints
apply to azd and CI as well as to direct SDK access.

### Configure scaling and operational visibility

Use HTTP scaling for the UI/API and explicit event/queue scaling for workers;
an ingress-disabled process does not receive an HTTP wake-up request. Configure
the scaler's managed-identity permissions, minimum/maximum replicas, worker
concurrency, lease renewal, and quota budgets. Follow current revision-mode
requirements for non-HTTP scale rules. Do not assume zero replicas will consume
queued work without a correctly configured trigger.

For a scheduled live presentation, a small approved minimum replica count and
reference-cache warm-up can avoid cold starts; measure rather than promise
latency. Keep backend state independent of UI sessions and account for WebSocket
disconnects or revision changes.

Show acknowledgement latency, queue wait, evaluation time, commit/visibility
time, coverage, held/rejected counts, retries, and estimated versus actual model
usage. Model/queue failures must be visible even if the UI itself is healthy.
Keep a centralized evidence/operation store rather than concurrent writes to a
shared `lineage_log.jsonl`.

Deployment references:
[Container Apps deployment](https://learn.microsoft.com/azure/container-apps/tutorial-code-to-cloud),
[azd project/command requirements](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-commands),
[Container Apps authentication](https://learn.microsoft.com/azure/container-apps/authentication),
[health probes](https://learn.microsoft.com/azure/container-apps/health-probes),
[scaling](https://learn.microsoft.com/azure/container-apps/scale-app),
and [storage lifetime](https://learn.microsoft.com/azure/container-apps/storage-mounts).

## 9. Rehearse, preserve evidence, and clean up deliberately

The demonstration is ready only when the displayed claims correspond to actual
behavior in the selected profile:

| Rehearsal | Required observation |
| --- | --- |
| Scenario parity | UI and headless client produce the same detector evidence for identical data/configuration |
| Publication | Gated flagged/incomplete records never appear in the actual published-data view; post-write mode visibly permits earlier exposure |
| Duplicate/restart recovery | Retried submission returns the existing operation; accepted work survives a process restart without duplicate effects |
| Failure semantics | Unavailable model/baseline, lost lease, and unknown commit outcome show explicit states, not green success |
| Coverage | Missing cloud-only checks and insufficient cohorts are named; replay/synthetic results are labeled |
| Permission boundary | Unauthorized users/agents cannot inspect other runs, author decisions, or bypass the publisher; local shared-user limitations remain explicit |
| Cloud network path | Real UI/API/worker/model/tool traffic works on approved paths and denied public/wrong-identity paths remain denied |
| Agent memory | Every registered agent persists/recalls through its scoped Cosmos path; other agents/tenants, pending content, revoked sources, expired memories, and stale concurrent updates cannot bypass policy |
| Live explanation | Serving model and tool actions come from actual responses; explanations cite recorded signals and do not invent provenance |
| Controlled load | Bounded synthetic bursts show queueing/backpressure without disabling detectors or exceeding approved quotas |

Retain a sanitized run bundle with IDs, timestamps, versions, thresholds,
findings, and outcome receipts for the demonstration. Retain originals only
under the approved access/retention policy. Do not keep raw live prompts just to
produce impressive screenshots.

At session end, stop admission, drain or explicitly resolve pending operations,
and stop the local/demo workers you own. A reset must target only the named demo
environment/run after confirmation; do not truncate shared databases or delete
an entire repository or workspace. Preserve evidence needed for recovery.

For Azure, review the created-resource inventory and ongoing costs. Scale down
where appropriate and remove only approved demonstration resources after retention
and ownership checks. `azd down` is destructive and can remove template-managed
resources; never run it automatically or against a template containing shared
resources without explicit approval. Cloud deployment, cleanup, and evidence
retention are distinct decisions.

Agent memory has its own retention, revocation, and backup lifecycle. Do not wipe
all agents' memory as a demo reset or assume TTL removes every derived/cache/
backup copy. Cleanup must name the approved environment and agent/tenant/run
scope, and preserve operation receipts required for recovery.
