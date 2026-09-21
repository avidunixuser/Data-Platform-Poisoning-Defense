# Enforcement modes, asynchronous writes, and scale

This is an integration playbook, not a deployed write gateway. The bundled
detectors return evidence; the connectors read data; `record_batch` writes a
local JSONL record. None intercepts another process's database writes.
`post_write_audit`, `inline_gate`, and `async_gate` are architectural labels, not
function arguments, command-line switches, or new runtime implementations.

## Choose the visibility and latency contract

| Mode | When detection happens | Client experience | Main trade-off |
| --- | --- | --- | --- |
| `post_write_audit` | After a production write, using captured changes or a scheduled audit | Existing write latency; findings arrive later | Exposed data may already have influenced retrieval, analytics, or training |
| `inline_gate` | Inside a mandatory application write boundary, before the target write | Wait for validation and the write result | Prevention requires bounded synchronous work and adds latency |
| `async_gate` | After durable acceptance into isolated staging, before production publication | Quick `202 Accepted`, then a separate final outcome | Low acknowledgement latency, but delayed visibility and additional operational machinery |

Recommend asynchronous gating when fast acknowledgement and validation before
publication are both required. It does not make an immediate production commit
safe while validation is pending. If the caller requires immediate visibility,
choose inline validation or explicitly accept post-write exposure.

Before implementation, establish the target store, allowed mutation types, all
writer paths, trusted references, mandatory detectors, reviewer authority,
per-tenant load, acknowledgement and publication SLOs, and status-retention
requirements. Ask for unresolved decisions. Do not provision queues, change
permissions, or enable write access merely because a user selected a design.

## Post-write flagging: detective, not preventive

```text
Authorized producer -> production commit
                    -> durable change event / checkpointed audit
                    -> validation worker pool
                    -> versioned finding + review/notification
```

Use approved CDC/change feeds, an application outbox, or bounded periodic reads.
The supplied SQL/Kusto loaders are not CDC subscribers. Verify each source's
update/delete coverage, retention, ordering, delivery semantics, and snapshot
behavior; there is no universal change-feed adapter in this repository.

Attach findings to stable source IDs, committed versions or immutable snapshots,
payload hashes, and model/reference/policy versions. Reading the latest row after
receiving an older event does not establish what that older write contained.
When the exact version is unavailable, report that coverage gap rather than
attribute an old event's verdict to the current row.

Persist flags in a protected findings store and alert the responsible reviewer.
Removing or excluding an already indexed item, rolling back a record, or
retraining a model requires an approved remediation workflow. A flag alone
neither reverses a commit nor removes downstream copies.

Track commit-to-detection lag, missed/expired event coverage, and time to
remediation. Record a failed audit as unevaluated; the original write remains
committed, so "fail closed" cannot retrospectively prevent its consumption.

## Inline interception: a trusted application write gate

```text
Agent/client proposal -> authenticate + authorize + validate schema
                      -> all required detector/policy checks
                      -> approved: conditional/transactional target write
                      -> flagged/incomplete/error: reject or hold, no target write
```

Only the trusted publisher/gate should hold the required production write
permissions. Agents and other producers must not retain a bypass through direct
SDK calls, SQL tools, alternate ingestion jobs, or admin credentials. Apply
authorization to every supported mutation, including updates/upserts and bulk
paths. Deletes and schema changes need operation-specific policy; a content
anomaly score is not authorization to perform them.

Validate the exact proposed mutation and its expected target version after all
relevant transformations. Do not trust a client-supplied `approved` field.
Bind approval to the payload hash, target/base version, and required detector,
reference, model, and policy versions. Changed data or invalidated approvals
require re-evaluation, not reuse of a previous clean result.

Use datastore-supported transactions or conditional writes to prevent a
validation-to-commit race. Where an index/store lacks the required atomic
operation, design a controlled publisher, version ledger, and reconciliation or
approved-version indirection. Do not imply that SQL, Cosmos DB, Search, Fabric,
PostgreSQL, and ADX share one cross-service transaction or commit hook.

Bound request size and validation time. Run cheap authorization/schema checks
before expensive scoring. Rejecting early is safe; a negative cheap check must
not skip another required layer and allow publication. A timeout, unavailable
baseline, API failure, or incomplete detector result cannot become an allow.
If required cohort analysis cannot finish in the inline budget, hold the write
or offer the explicitly agreed async contract; do not silently switch to audit.

## Asynchronous gated writes: acknowledge now, publish after validation

```text
Agent/client -> intake: auth + schema + size + idempotency
            -> immutable staging + operation ledger + reliable dispatch intent
            <- 202 Accepted + operation ID + Location + Retry-After

Durable work queue -> bounded validation worker pool -> decision ledger
                                                    -> held: reviewer/agent triage
                                                    -> rejected/failed: status
                                                    -> approved: trusted publisher
                                                               -> target write
                                                               -> final status
```

### Intake and client contract

1. Authenticate and authorize the proposed operation, validate basic shape/size,
   and enforce tenant admission limits before accepting work.
2. Durably persist the immutable candidate, operation metadata, and dispatch
   intent. Use a transactional outbox or equivalent recoverable handoff so a crash
   between persistence and enqueue cannot lose accepted work. Apply this to
   **pending staging**, not an unvalidated production write. Respect the chosen
   store's transaction scope; Cosmos DB transactional batches are partition-scoped.
3. Return `202 Accepted` only when durable intake is confirmed. Include an opaque
   operation ID, an authenticated status URL in `Location`, and a `Retry-After`
   polling hint. Do not return a success-shaped "committed" response. If intake
   durability is uncertain, resolve it through the idempotency ledger.
4. Let the client poll or use an authorized completion-notification channel.
   A status-endpoint `200 OK` means the status was retrieved, not that the mutation
   succeeded. Define retention/expiry and do not interpret a missing/expired
   status resource as success.

The initial call still waits for authorization and durable acceptance, not for
the entire detection pipeline. Merely starting a background task or using
fire-and-forget SDK calls is not durable acceptance.

Staging is itself a storage write, but it is **not production publication**.
Keep pending/rejected content out of retrieval indexes, agent-readable views,
feature tables, and training inputs. Prefer isolated staging resources and
permissions; a `pending` label is insufficient if any reader can ignore it.
Clients requiring read-your-write visibility must wait for the appropriate final
state rather than read unvalidated staging through the normal data interface.

### Processing, publication, and status

Workers read the exact staged version and pinned evaluation context, execute all
required checks, and persist structured evidence. Only the trusted publisher
acts on a valid decision from the protected ledger. Recheck current authorization,
approval validity, and target-version preconditions before writing. Persist the
committing state before issuing the target write so recovery can recognize an
uncertain outcome rather than assuming the operation was never attempted.

Use an explicit state model, for example:

| State | Meaning |
| --- | --- |
| `pending` / `running` | Durably accepted / being evaluated; not published |
| `held` | Awaiting missing context or authorized review; not published |
| `rejected` | Policy denies publication; not published |
| `approved` / `committing` | Eligible under recorded policy / write in progress; not yet confirmed committed |
| `committed` | Exact target version is confirmed written; return its identifier/version |
| `failed` | Terminal processing failure with a known non-commit outcome |
| `reconciling` | Write outcome is uncertain; do not blindly retry or claim success/failure |

These states are a suggested application contract, not fields added to the
existing detector results. Expose timestamps and sanitized error/reason codes.
Track downstream indexing/serving readiness separately when it lags the target
commit; committed does not necessarily mean query-visible everywhere.

A cancellation request is not proof that a racing commit was prevented. Mark an
operation cancelled only after proving it cannot publish; an already committed
operation needs a separate authorized compensating action.

### Retry and concurrency correctness

- Require an idempotency key scoped to tenant and operation. Repeated submissions
  with the same key and payload return the existing operation; a changed payload
  under the same key is a conflict, not a second write. Retain deduplication
  evidence for the supported client-retry and redelivery windows; document what
  happens to submissions outside those windows rather than promising unlimited
  duplicate protection.
- Assume at-least-once delivery. Use durable deduplication and target-side
  idempotency/conditional writes; broker duplicate detection alone is not an
  exactly-once guarantee for database side effects.
- Renew work leases within bounded job deadlines and fence stale workers. A lost
  message lock or stale worker must not publish an obsolete decision.
- Persist the result and confirmed write outcome before completing the work
  message. A database commit and broker acknowledgement are not generally atomic:
  reconcile a redelivery against durable state before applying the mutation again.
- Retry transient errors with capped exponential backoff, jitter, and deployment
  rate limits. Route exhausted/permanent processing failures to an operational
  dead-letter path and update status. Suspected poisoning belongs in a review
  path; a broker "poison message" is not proof of malicious data.
- Preserve per-entity ordering or expected-version checks for dependent mutations.
  Scale independent entities in parallel, not competing updates to the same record
  without a concurrency policy.
- Bound queue age, retry count, and retention. An expired job must produce an
  explicit held/failed outcome when non-commit is known, or reconciliation if a
  write may have occurred, never an automatic approval.

## Scale with validation workers and a separate agent pool

Use ordinary, bounded Python workers for numerical scoring and embedding API
calls. Their jobs and results can be reproducible and their resource use measured.
Use an optional **agent pool** for held-case investigation: gathering authorized
lineage evidence, comparing explanations, and drafting reviewer recommendations.
Do not launch an unconstrained LLM agent for every row or let an investigating
agent grant itself publication authority.

Scale these pools independently. Agent investigations need their own concurrency,
token/cost, context-size, tool-access, and time limits. Store progress/evidence
durably and keep suspect content non-authoritative. A proposal from an agent is
not a trusted gate decision.

On Azure, a Service Bus queue with competing consumers hosted in Azure Functions
or Container Apps is one implementation option. A change stream or Event Hubs
may feed post-write auditing where appropriate. These are suggestions, not
provisioned resources or a requirement to add messaging SDKs to the detectors.

### Preserve detector semantics while optimizing

| Optimization | Guardrail |
| --- | --- |
| Reuse fitted embedding references and semantic seed embeddings per worker | Key caches by tenant/dataset, model and transform versions, reference snapshot, and policy/threshold/seed versions. Warm them before readiness; invalidate changed context. |
| Keep SDK clients alive for a worker lifetime | Follow client concurrency/ownership rules and close only owned resources. Limit in-flight embedding calls across the entire pool, not only per process. |
| Separate CPU-heavy work from network I/O | Existing detector and SDK-facing APIs are synchronous. An `async` handler does not make them nonblocking; use bounded workers/executors or a reviewed async adapter. Limit BLAS/process oversubscription. |
| Microbatch compatible work with a maximum size and wait time | Preserve tenant, dataset, feature/model, reference, and policy boundaries. Cohort-dependent detectors need the agreed complete context, not arbitrary queue chunks. |
| Amortize semantic request overhead | Reusing one `SemanticInjectionClassifier` caches its seeds. Its current `score` API still sends one document per call; bulk embedding would require an adapter preserving alignment, limits, and failures. |
| Parallelize independent evaluation cohorts | Do not merge datasets merely to fill a batch, mix incompatible embedding spaces, or create a contaminated baseline from incoming candidates. |

**Do not partition k-NN label-flip analysis by label:** homogeneous-label shards
can erase the very disagreement being measured. Preserve representative
multi-class neighborhoods and map every result back to original IDs. Spectral
analysis also needs sufficient per-class context; its cross-class comparison
needs four evaluable classes. Defaults require at least six rows for k-NN with
five neighbors and ten rows per spectral class. Meeting these minima alone does
not establish statistical adequacy.

If the bounded wait expires before required context exists, keep those items held
or report incomplete coverage; do not silently omit a check and publish. Sampling,
approximate-neighbor indexes, incremental/sketched covariance, or distributed SVD
change the evaluation procedure and need a separately validated implementation
and recalibration. They are not switches exposed by the bundled code.

### Size the pools from measurements

Measure each route separately under representative dimensions, document lengths,
class distributions, and cold/warm cache conditions. A starting capacity estimate
for independent batches is:

```text
C >= ceil(lambda_peak * T_batch / (B_effective * utilization_target))
```

`C` is concurrent batch-processing slots, not agents; `lambda_peak` is accepted
items/second, `T_batch` is measured mean processing seconds per batch excluding
queue wait, and `B_effective` is observed new candidate items resolved per batch,
not the configured maximum or additional reference/context rows. Choose a
utilization target below one for headroom. Convert slots to instances only after
measuring safe per-instance concurrency, CPU, memory, and downstream limits.
This estimate is not a p95/p99 latency guarantee.

Autoscale on oldest queued age, queue depth, arrival rate, and processing time
within fixed minimum/maximum bounds. Cap the aggregate embedding request/token
rate and publisher throughput at their actual quotas/capacity. More workers do
not solve a throttled embedding deployment or an overloaded database.

Apply per-tenant fairness, payload/queue limits, and admission backpressure.
Return an explicit overload response, such as `429` or `503` with retry guidance,
when work cannot be accepted durably within policy. Never claim acceptance for
dropped work or disable required detectors to keep latency low. A sustained
arrival rate above service capacity grows the queue despite asynchronous APIs.

## Operational evidence and rollout

Track acknowledgement latency separately from queue wait, validation time,
commit time, serving visibility, and review time. Monitor p50/p95/p99 values,
accepted-to-final-state age, queue/DLQ depth, retry/lock-loss/throttling rates,
worker saturation, publication conflicts, detector coverage, false positives,
and cost per evaluated item. Post-write mode also needs commit-to-flag exposure
metrics. Set workload-specific targets; this repository supplies no throughput
or zero-latency guarantee.

Use a protected centralized operation/decision store and audit sink, or an
approved collector for worker-local logs. `LineageAuditor`'s single-writer JSONL
file is not a shared distributed ledger or a transaction coordinator. Do not
have a worker pool append concurrently to one local file. Queue envelopes and
logs should contain minimal opaque references, not credentials or raw sensitive
documents; secure staged payloads and enforce retention.

Before enabling enforcement, exercise crash-after-intake, dispatch failure,
duplicate/out-of-order delivery, lost locks, unavailable baselines, partial
detector coverage, embedding throttling, commit-before-ack crashes, uncertain
commit outcomes, cancellation races, stale approval, queue exhaustion, and
attempted direct-write bypass. Verify every accepted operation remains
accounted for and unapproved content cannot be read by serving/training paths.
Shadow/post-write rollout can measure behavior, but its exposure must be explicit;
do not describe shadow auditing as active prevention.

## Sources and related guidance

- [Asynchronous Request-Reply](https://learn.microsoft.com/azure/architecture/patterns/asynchronous-request-reply): acceptance, operation status, polling, and idempotent submission.
- [Queue-Based Load Leveling](https://learn.microsoft.com/azure/architecture/patterns/queue-based-load-leveling): buffering, backpressure, downstream limits, and queue durability.
- [Competing Consumers](https://learn.microsoft.com/azure/architecture/patterns/competing-consumers): bounded worker pools, ordering, and repeated delivery.
- [Service Bus transfers, locks, and settlement](https://learn.microsoft.com/azure/service-bus-messaging/message-transfers-locks-settlement): explicit send outcomes, receive locks, acknowledgements, and retries.
- [Transactional Outbox with Cosmos DB](https://learn.microsoft.com/azure/architecture/databases/guide/transactional-out-box-cosmos): reliable dispatch within the store's supported transaction scope.
- [Detector calibration](detection_calibration.md) and [connector contracts](data_connectors.md): existing thresholds, cohort requirements, and read-only loader behavior.
