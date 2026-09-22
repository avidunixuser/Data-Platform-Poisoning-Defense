# Microsoft Foundry implementation and private-network guide

**Architecture guidance; documentation reviewed 2026-09-21.** This is not
deployable IaC or evidence of a live network/RBAC validation. The repository
contains detectors and instructions, not the API, MCP server, queue processors,
publication service, or Azure infrastructure described below. Obtain approval
for the actual subscription, region, topology, permissions, and rollout before
creating resources or changing production access.

This guide targets an Azure public-cloud, single-region implementation of
`async_gate`: acknowledge durable intake, validate in background workers, and
publish only an approved mutation. See [enforcement and scaling](enforcement_and_scaling.md)
for the post-write and inline alternatives, state machine, and failure handling.
Do not silently substitute post-write detection for pre-publication enforcement.

## 1. Confirm the implementation contract

Record the approved tenant/subscription, resource groups, region, data residency,
network owner, existing hub/spokes, nonoverlapping address ranges, DNS authority,
client access paths, and private CI/CD runner location. Decide the Foundry agent
type/network setup and verify the exact regional model, SKU, tool, and network
combination before selecting a template.

Also establish allowed mutations and all producer paths, target resource IDs,
reference/model/policy versions, review authority, acknowledgement versus
publication SLOs, retention, backup/recovery requirements, and cancellation
semantics. Use synthetic data for initial acceptance. No client or agent should
receive production write authority merely because it can submit a candidate.

Private connectivity, identity, and application authorization are independent:
a private IP does not grant access; a valid token does not create a network
route; a successful MCP/A2A invocation does not approve a database mutation.

## 2. Inventory services and trust boundaries

The earlier application-only count of nine families did not include every
secured Foundry dependency. **This Standard BYO-VNet profile uses ten logical
application/platform service families, including Entra ID and Azure AI Search.**
It is not ten resource instances or a complete private-network bill of materials.

| # | Service family | Implementation responsibility |
| --- | --- | --- |
| 1 | Microsoft Foundry | Proposing/investigating agents, model and embedding deployments, project connections, and optional toolboxes/A2A endpoints |
| 2 | Azure Container Apps | Separate intake/status/MCP, validation-worker, and trusted-publisher workloads and identities |
| 3 | Azure Container Registry | Versioned images for those workloads; private image-pull and build paths |
| 4 | Azure API Management | Governed API entry point and, where the selected route supports it, REST-to-MCP exposure |
| 5 | Azure Service Bus | Validation/publication work queues, bounded consumers, retries, and dead-letter handling |
| 6 | Azure Blob Storage | Isolated candidate staging, immutable trusted reference artifacts, and retained evidence |
| 7 | Azure Cosmos DB for NoSQL | Separately permissioned admission/outbox, validation-decision, and publication-receipt data; an approved SQL design could substitute |
| 8 | Azure Monitor | Application Insights, Log Analytics, diagnostic settings, and private telemetry connectivity where required |
| 9 | Microsoft Entra ID | Caller/service authentication, managed identities, application roles, and privileged deployment governance |
| 10 | Azure AI Search | Required backing service for the secured Standard Foundry setup, even if the poisoning-defense application has no Search target |

The final inventory also needs VNets/subnets, private endpoints, private DNS,
hybrid connectivity/resolvers where applicable, and egress controls. Key Vault
is needed when secrets, certificates, or customer-managed keys require it.
Secured Standard Foundry requires Storage, Cosmos DB, and Azure AI Search
connections. Foundry state/backing resources are not automatically interchangeable
with candidate staging or the trusted operation ledger. Use distinct resources
or rigorously separated scopes; if a platform identity needs account-wide access,
do not place the gate's protected decisions in that account. Multiple accounts
of one product still count as one service family. Existing protected business
datastores are separate targets, not a requirement to deploy all six.

At the application layer, keep this flow independent of agent reasoning:

```text
Authorized client / Foundry agent
  -> authenticated submission API or domain-specific MCP facade
  -> immutable candidate + admission/outbox -> durable queue
  -> validation worker -> protected decision -> trusted publisher
  -> conditional target write -> protected receipt / status

Held cases -> authorized reviewer / optional investigation agent or A2A peer
```

## 3. Build and lock down the private network

### Choose one supported profile

Use **Foundry Standard Agent Setup with BYO VNet injection** for this baseline.
An account private endpoint protects inbound access; it does not alone isolate
agent egress. Outbound isolation also needs the delegated agent subnet, private
dependencies, correct DNS, and explicit egress controls.

Plan injection when creating the account. Current documentation does not support
adding injection later or moving it to another subnet; an existing incompatible
account needs a reviewed migration rather than an assumed in-place change.
Do not mix this profile with Foundry classic hub-based managed-network settings.
Use a current official Standard/private-tool template as a starting point, but
review its privileges, dependencies, and defaults before parameterizing it.

Private-MCP documentation has differing Basic-versus-Standard wording. This
guide deliberately selects Standard rather than relying on that discrepancy.
The documented tested private MCP host is an internal Azure Container Apps
environment on a dedicated tools subnet.

Sources: [Foundry network isolation](https://learn.microsoft.com/azure/foundry/how-to/configure-private-link),
[Agent Service VNets](https://learn.microsoft.com/azure/foundry/agents/how-to/virtual-networks),
and [official private-tool sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/19-private-network-agent-tools).

### Reserve subnets before provisioning platform resources

Use approved, nonoverlapping address ranges with capacity for replicas, revision
overlap, private-endpoint IPs, and growth. The sizes below are documented minima
or recommendations, not an address plan for the user's network.

| Subnet purpose | Delegation | Constraint for this profile |
| --- | --- | --- |
| Foundry agent runtime | `Microsoft.App/environments` | Dedicated to one Foundry account; /27 minimum, /24 recommended; Foundry and VNet in the same region |
| Internal ACA application/MCP environment | `Microsoft.App/environments` | Separate workload-profiles environment subnet; /27 minimum, enlarge for scale; never reuse the Foundry agent subnet |
| APIM Standard v2 outbound integration | `Microsoft.Web/serverFarms` | Dedicated to one APIM instance; /27 minimum, /24 recommended; same subscription and region as APIM |
| Private endpoints | None | Separate nondelegated subnet, sized for actual endpoint IP consumption |
| Operator/CI, resolver, firewall, or gateway subnets | Per selected service | Provision only those required by the approved connectivity/egress design; follow each service's reserved-name and sizing rules |

Link/peer the required VNets and provide approved VPN/ExpressRoute or equivalent
private operator access. Peering does not automatically provide private DNS
resolution. Subnet NSGs do not provide per-agent authorization, and workloads
sharing an ACA environment may need separate environments/subnets when stronger
network or egress isolation is required.

Sources: [ACA custom VNets](https://learn.microsoft.com/azure/container-apps/custom-virtual-networks)
and [APIM outbound integration](https://learn.microsoft.com/azure/api-management/integrate-vnet-outbound).

### Configure private application ingress without accidental isolation gaps

Create an **internal workload-profiles ACA environment**. For an MCP/API app
called from outside that environment but inside the approved VNet path, enable
external-to-environment app ingress; the environment's internal load balancer
still makes the application privately reachable, not publicly exposed.
Environment-only app ingress can prevent Foundry or APIM in another environment
from reaching it. Keep ingress disabled on workers that only consume queues.
Review environment-level routes before assuming an internal-ingress setting
alone makes an app unreachable through every other route.

For the internal environment's default-domain DNS, obtain its actual
`properties.defaultDomain` and `properties.staticIp`. Create/link a private zone
for that returned domain and a wildcard A record pointing to that private load
balancer address. Do not invent the generated domain or private IP.

ACA environment **Private Link is a different design**: subresource
`managedEnvironment`, zone `privatelink.<region>.azurecontainerapps.io`, supported
for workload-profiles environments with public access disabled. That PE's IP is
not the internal load balancer's `staticIp`. Do not combine the two DNS recipes
or add an environment PE merely because the chosen internal-LB profile is private.

Use **APIM Standard v2 with an inbound gateway private endpoint and outbound VNet
integration** where APIM is selected. This combination is documented. Disable
public gateway access and configure backend DNS, routing, and authentication.
The inbound PE does not create outbound reachability and covers the gateway,
not every management/developer-portal surface. Inbound NSGs on its integration
subnet are not the gateway's ingress-enforcement mechanism.

Do not mix this profile with classic internal VNet injection: classic injected
APIM instances cannot combine that mode with inbound private endpoints. Also,
creating an API gateway through Foundry's UI can produce a public gateway even
when Foundry is private; private settings are not inherited automatically.

For initial validation, use **Foundry -> direct private ACA MCP**, and use APIM
for private client intake/status APIs. Route Foundry through APIM-generated MCP
only after private discovery, authentication, tool execution, and transport tests
pass for the chosen combination. Support for each feature separately is not proof
that the combined private route has been validated.

Sources: [ACA ingress](https://learn.microsoft.com/azure/container-apps/ingress-overview),
[ACA PE/DNS](https://learn.microsoft.com/azure/container-apps/private-endpoints-with-dns),
and [APIM private endpoints](https://learn.microsoft.com/azure/api-management/private-endpoint).

### Create and approve each required Private Endpoint and DNS zone group

Private Link is the service connectivity mechanism; a private endpoint is a
specific network interface/connection to a particular resource and subresource.
Create endpoints for the actual accounts/namespaces in use, approve their
connections, associate the correct private DNS zones, and link/forward those
zones for all intended callers.

| Resource | PE subresource | Azure public-cloud private DNS zone(s) / requirement |
| --- | --- | --- |
| Foundry / Azure OpenAI account | `account` | `privatelink.services.ai.azure.com`, `privatelink.openai.azure.com`, `privatelink.cognitiveservices.azure.com` as applicable to the endpoints actually used |
| Blob staging, references, or Foundry backing storage | `blob` | `privatelink.blob.core.windows.net`; add separate subresources/zones for other storage services actually used |
| Cosmos DB for NoSQL | `Sql` | `privatelink.documents.azure.com`; dedicated gateway is a different subresource |
| Service Bus namespace | `namespace` | `privatelink.servicebus.windows.net`; Premium required |
| Container Registry | `registry` | `privatelink.azurecr.io`; Premium required; include registry and regional data endpoints |
| API Management gateway | `Gateway` | `privatelink.azure-api.net`; use the supported profile above |
| Foundry backing Azure AI Search | `searchService` | `privatelink.search.windows.net`; Basic or higher, not Free |
| Key Vault, when used | `vault` | `privatelink.vaultcore.azure.net` |
| Azure Monitor Private Link Scope, when private telemetry is required | `azuremonitor` | `privatelink.monitor.azure.com`, `privatelink.oms.opinsights.azure.com`, `privatelink.ods.opinsights.azure.com`, `privatelink.agentsvc.azure-automation.net`, and `privatelink.blob.core.windows.net` |

A separately provisioned embedding account needs its own private connectivity.
ACR image pulls also require the regional data endpoints, not just login to
`<registry>.azurecr.io`. With Azure Private DNS, put the required data records
in the existing `privatelink.azurecr.io` zone rather than inventing regional data
zones; include all used replicas/endpoints and reserve their additional IPs.

Keep canonical service FQDNs and normal TLS/SNI validation in application
configuration. DNS should resolve them to approved private destinations.
Do not substitute private IPs or private-link aliases in SDK connection strings
or disable certificate validation. The built-in embedding and Kusto adapters
allow Azure public-cloud hostnames; Private DNS does not require loosening their
hostname validation. Sovereign-cloud domains require a separately reviewed
provider/configuration, not reuse of this table.

Use central DNS ownership to avoid conflicting zones. Forward documented service
domains from connected networks to an Azure DNS forwarder/Private Resolver,
and verify the full resolution chain from actual callers. Do not rely on public
DNS fallback when a required private record is missing.

Sources: [PE DNS configuration](https://learn.microsoft.com/azure/private-link/private-endpoint-dns),
[DNS integration](https://learn.microsoft.com/azure/private-link/private-endpoint-dns-integration),
[ACR Private Link](https://learn.microsoft.com/azure/container-registry/container-registry-private-endpoints),
and [Service Bus Private Link](https://learn.microsoft.com/azure/service-bus-messaging/private-link-service).

### Close alternate ingress and control egress

1. Verify each endpoint is approved and resolves/routes correctly before migration
   cutover. For new resources, prefer public data access disabled from creation
   where the supported provisioning path permits it.
2. Set each service's public-network control explicitly. PE creation alone does
   not disable public access. Review IP allowlists, trusted-service exceptions,
   and alternate endpoints; do not enable broad exceptions to repair a failed
   private route.
3. Apply reviewed NSGs, UDRs, and an egress enforcement point where required.
   Enable PE subnet network policies before relying on NSG/UDR enforcement for
   those interfaces. A `0.0.0.0/0` route alone does not override PE routes.
4. Allow only the required private service flows plus documented platform,
   identity, certificate, and operational dependencies. Private data-plane
   access is not a claim that Entra or the Azure control plane is air-gapped.
   Avoid public web/Bing-grounding tools in the strict baseline.
5. Secure telemetry independently. Configure AMPLS topology/access modes and the
   individual Application Insights/Log Analytics public ingestion/query settings.
   Plan one AMPLS per shared DNS context; follow Monitor's guidance on additional
   egress controls for public ingestion endpoints. Do not assume one private
   endpoint privatizes every monitoring path.
6. Test allowed and denied paths, including private CI, image layer downloads,
   agent-to-tool calls, and model inference. Document necessary exceptions with
   scope and ownership, rather than calling an exception-based path private-only.

Sources: [PE network policies](https://learn.microsoft.com/azure/private-link/disable-private-endpoint-network-policy)
and [Azure Monitor private-link design](https://learn.microsoft.com/azure/azure-monitor/fundamentals/private-link-design).

## 4. Configure managed identities and least-privilege authorization

### Separate principals before assigning access

Provision the approved accounts/namespaces and empty containers/queues before
assigning roles at their resource scopes; section 5 defines the data layout and
publication behavior. Do not start producers until access checks pass. The
official Standard Foundry setup also needs its own backing-service identities,
role assignments, and project connections on separately protected resources;
the custom application matrix below does not replace that platform setup.

1. Create distinct user-assigned managed identities for intake, validation, and
   publication. Use separate facade, APIM, image-pull, and telemetry identities
   where those components need different permissions or lifecycles. A facade
   can share intake's identity only when intentionally sharing that trust
   boundary; it must not share the publisher's identity.
2. Attach each identity only to its intended application/resource. Record its
   client ID, principal/object ID, resource ID, owner, and approved purpose:
   these identifiers are not interchangeable.
3. Bind application SDK credentials explicitly to the intended managed identity.
   For the existing embedding/Kusto adapters, retain
   `AZURE_AUTH_MODE=managed_identity` and use `AZURE_CLIENT_ID` to select the
   application UAMI. Do not introduce a production `DefaultAzureCredential`
   fallback that can silently select a developer identity.
4. Grant only the required data-plane rights at individual queue/container/API
   scopes. Grant broader management permissions to a separate deployment identity,
   never to a runtime identity to work around an SDK authorization failure.
5. Exercise the real SDK operations and token renewal under each identity. After
   those paths work, disable local/key authentication where the service supports
   it and all required callers have migrated. Do not put access tokens, keys, or
   secret-bearing connection strings in chat, source control, or audit records.

The following are practical built-in baselines, not assertions that every
Contributor role is minimal or append-only. Replace them with narrower reviewed
roles where the actual operations permit it.

| Runtime principal | Allowed data-plane operations and scope | Explicitly excluded |
| --- | --- | --- |
| Intake | `Storage Blob Data Contributor` on the staging container; native `Cosmos DB Built-in Data Contributor` on admission/validation-request outbox; `Azure Service Bus Data Sender` on the validation queue | Decision/receipt writes, publication-queue sends, trusted-reference updates, and production-target writes |
| Validator | `Storage Blob Data Reader` on staged payloads and trusted references; native `Cosmos DB Built-in Data Reader` on admission and `Cosmos DB Built-in Data Contributor` on decisions/publication outbox; `Azure Service Bus Data Receiver` on validation queue and `Azure Service Bus Data Sender` on publication queue | Production writes, baseline replacement, and fabrication of commit receipts |
| Trusted publisher | `Azure Service Bus Data Receiver` on publication queue; native `Cosmos DB Built-in Data Reader` on decisions/necessary admission data and `Cosmos DB Built-in Data Contributor` on receipts; `Storage Blob Data Reader` on approved payloads; reviewed target-specific write permissions | Authoring validation decisions or treating a queue message as sufficient approval |
| MCP/API facade | Custom application permissions for approved submission, investigation, or status operations; add datastore rights only when explicitly required by its implementation | Generic SQL/shell access, arbitrary target connections, approval-store mutation, and publisher credentials |
| APIM backend identity | The backend API's explicitly defined application permission and correct token audience | Direct business-database access or implicit authority to act as every end user |
| Foundry agent callers | Approved custom proposal/investigation API permissions for the actual agent principal or selected project MI | Direct mutation of production data, reference artifacts, decisions, or receipts |
| Application invoking a Foundry agent | `Foundry Agent Consumer` at the intended agent scope where supported | Agent/project management merely to invoke an endpoint |
| Telemetry emitter or collector | `Monitoring Metrics Publisher` on the specific Application Insights resource when using its Entra-authenticated ingestion path | Monitoring administration or an assumption that one role covers every logging/export mechanism |

Service Bus role assignments should target individual queues. Blob data roles
should target individual containers. Cosmos permissions are **native data-plane
role assignments**, scoped to paths such as `/dbs/<database>/colls/<container>`;
ordinary Azure ARM `Contributor` is not `Cosmos DB Built-in Data Contributor`.
Verify the role definition and scope instead of relying on similar display names.

Cosmos item fields or document types do not create an RBAC boundary. Separate
admission, trusted decisions, and receipts into independently permissioned
containers. Only co-locate an outbox with the records its owning component may
write, in the same container/partition when transactional batching is required.
The publisher must read the protected decision and verify its tenant, operation,
target, and payload binding; publication messages are notifications, not authority.

Built-in Contributor roles generally allow more than creation. For an append-only
requirement, review custom Cosmos data actions and the actual Blob upload calls
before granting a reduced role; a permission sufficient for a new block blob may
not support a multipart/overwrite SDK path. Protect trusted references with
runtime-read-only access and an appropriate immutability policy where required.
Reader RBAC alone is not a WORM guarantee.

Sources: [managed-identity credential selection](https://learn.microsoft.com/azure/developer/python/sdk/authentication/credential-chains),
[Service Bus identity/scopes](https://learn.microsoft.com/azure/service-bus-messaging/service-bus-managed-service-identity),
[Blob container scopes](https://learn.microsoft.com/azure/storage/blobs/assign-azure-role-data-access),
[Cosmos data roles](https://learn.microsoft.com/azure/cosmos-db/reference-data-plane-security),
[Cosmos native assignments](https://learn.microsoft.com/azure/cosmos-db/how-to-connect-role-based-access-control),
and [Application Insights authentication](https://learn.microsoft.com/azure/azure-monitor/app/azure-ad-authentication).

### Authorize model inference and image pulls separately

For a direct Azure OpenAI `text-embedding-3` deployment, the embedding guide
specifies **Cognitive Services OpenAI User** on the resource serving that
deployment. The v1 request's `model` value is the deployment name. Use the
documented token audience/provider for the selected endpoint; current v1 Python
guidance uses `https://ai.azure.com/.default`. Bind the renewable credential to
the validator's intended identity rather than using a fixed bearer token.

`Foundry User` is a project/platform role, not a universal inference permission
for every provider. Do not grant additional project management rights to repair
an inference failure without identifying the actual resource and required data
action. Other provider/deployment combinations require their own verification.

| ACR permissions mode | Runtime image-pull permission |
| --- | --- |
| RBAC Registry Permissions | `AcrPull`, scoped to the registry |
| RBAC Registry + ABAC Repository Permissions | `Container Registry Repository Reader`, assigned at registry scope with a condition restricting the required repositories |

`AcrPull` is not honored in ABAC-enabled registries. Repository Reader does not
include registry catalog listing; add listing only if the actual client needs
it. Runtime pull identities must not receive registry-admin credentials or push
rights. A separately authorized build identity owns publishing approved images.

Sources: [embedding authentication](https://learn.microsoft.com/azure/foundry/openai/how-to/embeddings),
[Foundry roles](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry),
[ACR ABAC](https://learn.microsoft.com/azure/container-registry/container-registry-rbac-abac-repository-permissions),
and [ACR built-in roles](https://learn.microsoft.com/azure/container-registry/container-registry-rbac-built-in-roles-overview).

### Enforce custom API and Foundry caller authorization

Azure resource RBAC does not automatically authorize a custom MCP/API operation.
Define application permissions such as `Proposal.Submit` and
`Investigation.Read` in the API's Entra application. These are illustrative
custom app roles, not Azure built-ins. Assign the intended service principals
to the appropriate app roles using separately authorized directory administration.

The API must validate token signature, issuer, tenant, audience, lifetime, and
caller authorization. Enforce application roles for app-only calls or delegated
scopes for delegated calls, plus tenant/record/mutation policy. Derive identity
from validated claims, not caller-supplied `agent-id`, tenant headers, or approval
fields. Tokens for Foundry, Storage, or another service are not automatically
valid for this custom API.

If APIM's `authentication-managed-identity` policy replaces an inbound token
with a backend token, that token represents **APIM**, not the originating user.
Authorize the original request and deliberately preserve the required caller
authorization through a reviewed delegation design. Never blindly forward
wrong-audience tokens or trust an unverified identity header. Treat APIM policy
editing as privileged because editors can exercise its managed identity.

Confirm the Foundry identity model actually deployed:

- In the new agent model, an agent has a unique Entra agent identity at creation;
  publishing does not change that identity.
- In the legacy Agent Application model, unpublished agents share a project-level
  agent identity and publishing creates a different identity requiring downstream
  grants.
- A project managed identity and an agent identity are different principals.
  Explicitly choosing project-MI authentication for an MCP connection creates a
  shared authorization boundary; an agent identity is not an application UAMI.

Do not infer the caller's principal ID from an agent name or publication label.
Confirm it from the supported identity/configuration interfaces and authorized
request evidence without logging bearer tokens. Treat project-connection editing
as privileged configuration, not a permission needed by an endpoint consumer.

Incoming Foundry A2A uses Entra authentication and `Foundry Agent Consumer` or an
appropriate role at the target agent/project scope; it is not an anonymous or
key-authenticated endpoint. Neither MCP tool approval nor an A2A peer's response
grants permission to release a held mutation.

Sources: [MI application-role assignment](https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/assign-app-role-managed-identity-azure-cli),
[token-claim validation](https://learn.microsoft.com/entra/identity-platform/claims-validation),
[APIM backend MI](https://learn.microsoft.com/azure/api-management/authentication-managed-identity-policy),
[agent identity migration](https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-agent-applications),
[agent versus project identity](https://learn.microsoft.com/azure/foundry/agents/concepts/agent-identity),
and [incoming A2A authentication](https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint).

### Keep deployment administration and target-write grants outside runtime

Use a separate deployment/IAM identity with approved resource-management
permissions and narrowly scoped, conditional **Role Based Access Control
Administrator** capability when role assignment is needed. Restrict the roles,
principals, and scopes it may assign. Do not give runtime identities Owner,
ARM Contributor, or role-assignment administration.

Cosmos-native role administration, Entra application-role administration, and
SQL/PostgreSQL database-principal provisioning are different authorization
systems. Do not assume one Azure IAM grant configures all of them. Before
enabling a publisher adapter, approve its exact target data-plane operations,
managed-identity support, native database grants, token-renewal behavior, and
network path. There is no universal "publisher" Azure role for all six targets.

The supplied SQL/Kusto loaders remain read-only. The loaders do not themselves
implement SQL/PostgreSQL Entra token acquisition/refresh. Configure and exercise
the chosen driver's built-in passwordless support or a reviewed token-provider
integration; do not assume an arbitrary environment URL enables it. Do not
disable TLS or introduce hardcoded passwords to make a private connection succeed.

Source: [conditional role-assignment delegation](https://learn.microsoft.com/azure/role-based-access-control/delegate-role-assignments-overview).

## 5. Build the durable data and publication boundary

Use separate Blob containers and data-plane permissions for untrusted staging,
approved references, and retained evidence. Stage immutable payload versions and
record hashes; neither the proposing agent nor intake identity may overwrite a
trusted baseline. Serving indexes, agent-readable views, and training jobs must
not read pending/rejected staging.

Do not put every Cosmos record in one broadly writable container. Separate
admission/outbox records, validation decisions, and publication receipts so the
intake path cannot forge an approval and investigators cannot rewrite commit
history. The status API can combine authorized read-only projections.
If using a Cosmos transactional outbox, put the admission record and its dispatch
intent in the same container and logical partition supported by that transaction.
Separate containers are not one atomic Cosmos transaction.

An immutable Blob upload and a Cosmos transaction are also not one distributed
transaction. Confirm staging durability, persist admission/dispatch intent, and
return `202 Accepted` only when recovery can account for the accepted work.
Handle orphaned staging through an approved retention/reconciliation process.

The intake API must derive caller identity from validated authentication, enforce
tenant/resource/mutation policy and size limits, and generate trusted metadata.
Ignore caller-supplied approval or reviewer fields. Bind idempotency to the
tenant, operation, payload hash, and expected target version.

Workers apply the existing detectors to approved references and complete
cohorts. Persist the payload and evaluation versions, scores, reasons, omitted
checks, and outcome. A flag, missing mandatory check, timeout, or model failure
cannot yield automatic publication.

Only the publisher can mutate protected target data. It verifies the trusted
decision, current authorization, payload hash, approval validity, and expected
target version immediately before the write. Use the target's supported
conditional/transactional operation; do not claim a transaction across all
datastores. Preserve uncertain outcomes as `reconciling`, not failed/succeeded
guesses, and reconcile redelivery before applying a mutation again.

## 6. Package and scale the application workloads

Build versioned images from the skill's declared dependencies and reviewed
service adapters. The application layer must supply APIs, durable state,
broker integration, role enforcement, and target-specific writes; copying
`SKILL.md` into a container does not implement those services.

Run a bounded pool of Python validation workers. The current detector and
embedding SDK calls are synchronous; moving them into an `async` function does
not itself make them nonblocking. Reuse versioned fitted references and semantic
seed embeddings per worker. Preserve statistical cohort semantics and enforce
embedding/token, CPU/memory, queue-age, and publisher-throughput budgets.
Use a separate agent pool only for investigations that need flexible reasoning.

Build/push/pull paths must remain usable after registry public access is disabled.
Use a private runner or another explicitly supported private build path; an
ordinary public CI runner does not gain private connectivity from an Azure
role assignment. Prefer workload-identity federation for deployment automation.
Do not install dependencies from arbitrary public feeds at production startup.

Deploy and warm the API, workers, and publisher with readiness/liveness checks,
versioned configuration, and graceful shutdown. Stop taking new work before
shutdown; finish or relinquish leases safely. Share durable state, not a local
JSONL file: `LineageAuditor.record_batch` is not a distributed transaction ledger.

## 7. Expose MCP tools and add A2A selectively

Keep skill instructions in the approved agent configuration/catalog. MCP exposes
executable operations; it does not automatically load or elevate `SKILL.md`.
Suggested domain tools are `submit_candidate`, `audit_committed_batch`,
`get_operation_status`, `get_findings`, and `request_review`. These names describe
APIs to implement, not existing tools supplied by this repository.

Expose only bounded schemas and approved source/target catalog entries.
Do not expose arbitrary SQL, shell execution, a generic database writer, or a
caller-controlled connection string to the proposing agent. A tool allowlist or
approval prompt is defense in depth, not a substitute for server authorization.
MCP submission should return an operation reference promptly; the durable job
continues separately. A completed tool call is not a committed database write.

API Management can expose selected REST operations as MCP tools, but this route
currently supports tools rather than MCP resources/prompts. A custom MCP facade
in Container Apps is another option. Use the verified private route for the
chosen Foundry setup, not an assumed public-gateway equivalent.

Foundry documents A2A v1.0 as generally available and lists A2A networking through
the VNet subnet. Incoming endpoints require the Responses protocol and support
text-only JSON-RPC without streaming. Explicitly select v1.0 rather than relying
on a preview default. Prefer
case-level delegation with opaque operation IDs and minimized evidence, not raw
datasets or one agent request per vector. An agent card, peer response, or task
completion is not publication authority.

A2A private reachability is a separate acceptance gate, not a claim that private
A2A is unsupported. The private-tool sample's preview A2A test/server versions
do not prove a GA v1.0 route works end to end. Keep A2A optional until the actual
private agent-card discovery and authenticated invocation pass in both required
directions, including any APIM intermediary. Verify the Entra audience and roles.
Do not reopen public access to make an unproven combination appear functional.

Sources: [Foundry MCP](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol),
[REST-to-MCP in API Management](https://learn.microsoft.com/azure/api-management/export-rest-mcp-server),
[A2A connections](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/agent-to-agent),
and [incoming A2A](https://learn.microsoft.com/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint).

## 8. Connect only the protected data platforms actually needed

Network closure must cover the data paths the application really uses, not just
one successful portal connection. Use the existing read-only
[connector contracts](data_connectors.md); publisher adapters are additional code.

| Target | Private-connectivity requirements and caveats |
| --- | --- |
| Cosmos DB for NoSQL | Use the API-specific private endpoint and canonical account hostname. Keep business-data permissions separate from admission, decisions, receipts, and trusted references. |
| Azure AI Search | Inbound private endpoint subresource `searchService`, zone `privatelink.search.windows.net`; Basic tier or higher. Disable public data access separately. Indexer outbound access is a different path requiring supported shared private links and explicit private execution. |
| Azure SQL Database | Private endpoint on the logical server, subresource `sqlServer`, zone `privatelink.database.windows.net`. Use `<server>.database.windows.net`, not its private IP or private-link alias. Deny public network access separately. SQL Managed Instance has a different network design. |
| Azure Database for PostgreSQL Flexible Server | For eligible public-access-mode servers, Private Link uses subresource `postgresqlServer` and private DNS. VNet-integrated private access is a different mode; current docs do not support adding private endpoints to VNet-integrated servers. Verify server eligibility before planning a migration. |
| Azure Data Explorer | Verify the cluster private endpoint's generated query, ingestion, and dependent Blob/Table/Queue DNS records and IP capacity. Keep canonical cluster connection strings. Cluster public access and service-initiated outbound dependencies are separate controls; private endpoints are not supported for VNet-injected clusters. |
| Microsoft Fabric Lakehouse | Fabric is SaaS, not a workload placed in this application VNet. Select tenant- or workspace-level Private Link, validate the exact Lakehouse/OneLake/SQL endpoint/API, and separately govern outbound managed private endpoints. Workspace restrictions do not cover every administrative API. |

For Search indexers, approve the outbound shared-private-link connection and set
`executionEnvironment` to `private`; automatic execution selection is not
sufficient. Confirm the selected source, skillset, SKU, model/billing endpoint,
and private-execution limits. An inbound Search private endpoint alone does not
privatize indexer reads of Blob, SQL, Cosmos, or model services.

For ADX, do not infer complete coverage from a successful query: ingestion uses
additional endpoints and transient storage. Choose supported managed private
endpoints for outbound paths where required. A documented trusted-service
exception is a distinct design choice, not equivalent to a private endpoint;
record and approve any exception instead of describing the path as fully private.

For Fabric, check the current supported-item matrix before blocking public
access. Unsupported items can prevent workspace restrictions, and some admin or
network-policy APIs require tenant-level controls. Use workspace-specific
connection information where required; do not invent a universal OneLake or SQL
private hostname. An inbound workspace private link does not replace outbound
Spark/connector protection or data authorization.

Sources: [Search private endpoints](https://learn.microsoft.com/azure/search/service-create-private-endpoint),
[Search indexer private access](https://learn.microsoft.com/azure/search/search-indexer-securing-resources),
[SQL Private Link](https://learn.microsoft.com/azure/azure-sql/database/private-endpoint-overview),
[PostgreSQL Private Link](https://learn.microsoft.com/azure/postgresql/network/concepts-networking-private-link),
[ADX private endpoints](https://learn.microsoft.com/azure/data-explorer/security-network-private-endpoint),
and [Fabric private-link support](https://learn.microsoft.com/fabric/security/security-workspace-level-private-links-support).

## 9. Prove the controls before cutover

Use synthetic payloads and a non-production target first. Record test time,
caller identity, source network, resolved addresses, target resource/version,
expected outcome, and actual outcome without logging credentials or raw data.

| Acceptance scenario | Required evidence |
| --- | --- |
| Private authorized access | The actual application/agent runtime resolves approved private addresses, validates TLS, and completes the required data-plane operation. A test VM alone is insufficient. |
| Public-path denial | The same authorized principal cannot use a disallowed public data-plane path. An unauthenticated failure alone does not prove network lockdown. |
| Identity denial | From an allowed private network, a valid wrong-role/wrong-tenant/wrong-audience identity cannot perform the protected operation. |
| Write separation | Proposing agents, intake, and validators cannot write production data; intake and investigators cannot forge approval or commit receipts. |
| Reference integrity | Runtime producers cannot modify trusted reference/model-policy artifacts. Unapproved versions cannot be substituted through request metadata. |
| Gate behavior | Flagged or incompletely evaluated candidates stay unpublished; staged data cannot appear in retrieval, analytics, or training. |
| Asynchronous recovery | Durable acceptance survives restart; duplicates, stale versions, expired leases, and commit-before-ack crashes do not create duplicate or unapproved effects. |
| Network failure | DNS, private endpoint, route, or embedding connectivity failures produce explicit pending/held/failed or reconciling status, not fail-open publication. |
| Protocol route | Real Foundry-to-MCP and any enabled A2A discovery/invocation succeed privately with scoped authorization; unsupported paths remain disabled. |
| Operations | Private image pull, deployment, identity token renewal, telemetry, alert delivery, and the approved recovery path continue to work after public access is disabled. |

After positive and negative evidence is approved, complete the public-access
and local-auth cutover for each resource, then repeat the tests. For new resources,
prefer private-only provisioning from the outset. Existing production resources
need a migration window, dependent-client inventory, and an approved recovery
plan; do not disrupt unrelated consumers.

Monitor policy drift, public-network settings, endpoint approvals, DNS changes,
role assignments, image versions, gate bypass attempts, queue age, failures,
detector coverage, and publication outcomes. Resource-policy checks do not
replace application-level acceptance. Revalidate after role, model, image,
network, or platform changes, and never use an automatic public-access fallback.
