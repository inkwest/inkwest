# Security Considerations

Inkwest uses AI to interpret cluster state and execute kubectl commands on your behalf. While it enforces read-only operations and integrates with Kubernetes RBAC, deploying any AI-assisted tool against production infrastructure requires careful evaluation of your own threat model.

## AI Limitations

Large language models can hallucinate, misinterpret output, or draw incorrect conclusions from cluster data. Always validate AI-generated analysis before taking action — especially when the suggested fix involves modifying resources, scaling workloads, or changing configurations outside of Inkwest.

Inkwest is a debugging assistant, not an autonomous operator. Treat its output as a starting point for investigation, not as authoritative diagnosis.

Like any AI-assisted tool, Inkwest can be subject to evasion. Crafted resource names, labels, annotations, or log output within a cluster could influence the AI's reasoning or steer it toward misleading conclusions. A compromised or swapped kubeconfig could redirect queries to an unintended cluster, causing the AI to operate on — and expose data from — the wrong environment without any visible indication. Mitigate this by treating kubeconfig files as sensitive secrets, validating cluster identity at the RBAC and network level, and never relying solely on Inkwest's output to confirm which cluster you are inspecting.

## Data Exposure to AI Providers

When you ask Inkwest a question, it executes kubectl commands and sends the output to your configured AI provider. This means cluster data — including pod names, environment variables, log contents, resource specs, namespace metadata, and events — may be transmitted to a third-party API.

Before deploying Inkwest, consider:

- **Sensitive data in logs**: Application logs may contain tokens, credentials, PII, or internal URLs. The AI provider will see whatever `kubectl logs` returns.
- **Environment variables**: `kubectl describe` output includes environment variables, which may contain secrets if not managed through Kubernetes Secrets or external vaults.
- **Resource names and namespaces**: Cluster topology and naming conventions are visible to the provider.
- **Provider data retention**: Review your AI provider's data retention and training policies. Some providers offer zero-retention options — use them for sensitive environments.

If data residency or confidentiality is a concern, consider using a self-hosted model, a provider with contractual data protection guarantees, or scoping Inkwest's access to non-sensitive clusters and namespaces.

## Read-Only Enforcement

Inkwest restricts the AI to a curated set of read-only kubectl operations: `get`, `describe`, `logs`, `events`, `top`, `explain`, and `api-resources`. Commands are validated before execution, and destructive operations (`create`, `apply`, `delete`, `exec`, `patch`, `edit`) are blocked at the application layer.

However, read-only enforcement in Inkwest is **defense in depth, not a single point of control**. You should also:

- Use dedicated Kubernetes ServiceAccounts with minimal RBAC permissions for each cluster Inkwest connects to.
- Restrict access to sensitive namespaces (e.g., `kube-system`, security tooling) at the RBAC level.
- Avoid granting `cluster-admin` or overly broad roles to Inkwest's ServiceAccount.

See [Kubernetes RBAC Setup](docs/KUBERNETES_RBAC_SETUP.md) for recommended configurations.

## Cluster Isolation and Segregation

Inkwest supports multiple clusters, but isolation between them depends on correct configuration:

- Each cluster connection uses its own kubeconfig. Ensure kubeconfigs are scoped to the intended cluster and namespace.
- OIDC role mappings control which users can access which clusters. Review these mappings regularly.
- In multi-tenant environments, ensure that a user's session cannot cross cluster or namespace boundaries beyond what their role permits.

Inkwest does not provide network-level isolation between cluster connections. If strict segregation is required, consider running separate Inkwest instances per security boundary.

## Authentication and Access Control

- **OIDC integration** is available for multi-user deployments. Without it, Inkwest runs in single-user dev mode with auto-login — suitable for local development only.
- **Session tokens** should be treated as sensitive. Use HTTPS in production to protect tokens in transit.
- **Admin access** grants visibility into all users, clusters, budgets, and usage. Limit admin roles to platform operators.

## Command Injection

All kubectl commands are constructed programmatically from validated parameters. User input is never interpolated directly into shell commands. The AI's tool-calling interface enforces structured inputs for resource type, name, namespace, and flags.

If you discover a bypass or injection vector, please report it responsibly (see below).

## Timeout and Resource Limits

kubectl commands are subject to a 30-second execution timeout to prevent hanging queries from consuming resources. Consider additional safeguards in production:

- Rate-limiting API requests per user
- Enabling budget controls to cap AI provider costs
- Monitoring Inkwest's resource consumption alongside your clusters

## Reporting Security Issues

If you discover a security vulnerability, please report it privately by opening a [GitHub security advisory](https://github.com/inkwest/inkwest/security/advisories/new) rather than a public issue. We will acknowledge receipt and work with you on a fix before any public disclosure.
