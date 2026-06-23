# Per-session EKS kubeconfig refresh

TL;DR — when a user selects AWS account/role pairs for a session, Inkwest
automatically discovers the EKS clusters those credentials can reach and writes one
**kubeconfig per cluster** into the session's RAM-only tmpfs. No disk persistence,
no `aws-sso-util`, no global `~/.kube`. In-process port of the `refresh-eks` shell
helper, scoped to a single session.

Builds on the AWS connector and the role filter
See [`CONNECTOR_AWS_SETUP.md`](./CONNECTOR_AWS_SETUP.md) and
[`CONNECTORS.md`](./CONNECTORS.md).

## Trigger

`POST /connectors/{session_id}/aws/select` mints STS credentials (one AWS profile
per chosen account/role) and stores them in the RAM credential store. Immediately
after, it fires `schedule_session_eks_refresh(session_id, cred)` — a fire-and-forget
`asyncio` task. The HTTP response is **not** blocked by cluster discovery.

## What it does

1. Projects the session profiles to the tmpfs `~/.aws` (`write_aws_profiles`) so the
   generated kubeconfigs can resolve credentials at `kubectl` time.
2. For each profile, in parallel (one thread per profile): `eks list-clusters` then
   `eks describe-cluster` via boto3, using that profile's STS credentials.
3. Collapses to **one entry per cluster** (keyed by ARN): the same cluster is often
   reachable via several selected roles in one account. When that happens, the role
   whose name mentions `eks` wins — a purpose-built `eks-*` role is the one mapped in
   the cluster's `aws-auth` / access entries, whereas a generic `*ReadOnly` role may
   be able to `DescribeCluster` (so it's discovered) yet have no in-cluster RBAC (its
   token would 403). Ties fall back to selection order, so the choice is deterministic.
4. Writes one kubeconfig per cluster and publishes them **in place** into the live
   session kube dir: each file is staged in a per-call sibling dir then renamed into
   the kube dir (per-file rename within the same tmpfs is atomic), and kubeconfigs for
   clusters no longer reachable are pruned. The kube dir's **inode is never swapped** —
   the per-conversation sandbox container bind-mounts it read-only and a bind mount
   follows the inode, so an `os.replace` of the *directory* would detach the live mount
   (the agent would keep seeing the pre-swap, empty dir). The publish+prune runs under
   a per-session lock so an overlapping re-select or a concurrent revoke can't leave a
   torn set.

Input = the session's AWS profiles. An account the selected role cannot reach is
logged and skipped (it never aborts the whole refresh).

## Output

- **Location**: the shared RAM creds volume `<sandbox_creds_root>/<session_id>/kube/`
  when a sandbox is configured (bind-mounted read-only into the conv container as
  `~/.kube`); otherwise the backend-local `/dev/shm/inkwest/<session_id>/kube/` tmpfs.
  Off-container (e.g. macOS dev/tests) falls back to a temp dir.
- **One file per cluster**, named `eks--<account_name>--<region>--<cluster>`. Mode is
  `0644` (dir `0755`) on the shared creds volume so the toolbox — which runs as a
  different uid than the backend — can read it through the read-only mount; `0600`
  (dir `0700`) on the backend-local `/dev/shm` projection. The read-only per-session
  mount, not the file mode, is the isolation boundary (same rationale as the `~/.aws`
  projection in `write_aws_profiles`).
- **Self-contained, secret-free**: each kubeconfig uses the standard AWS exec
  credential plugin (`aws eks get-token`). The exec block carries the AWS profile name
  and the `~/.aws` file paths — **no** access key / secret / session token is ever
  written into the kubeconfig. On a sandbox the paths are the **in-container**
  `<sandbox_home>/.aws/...` (where the creds are bind-mounted), not the backend write
  path; without a sandbox they are the backend's `/dev/shm` paths. Tokens are minted on
  demand by the plugin.

Example user/exec block (abridged):

```yaml
users:
  - name: arn:aws:eks:eu-west-1:…:cluster/prod
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1beta1
        command: aws
        args: ["--region", "eu-west-1", "eks", "get-token", "--cluster-name", "prod", "--output", "json"]
        env:
          # Sandbox: in-container paths (creds bind-mounted at <sandbox_home>/.aws).
          # Without a sandbox: the backend's /dev/shm/inkwest/<sid>/.aws/* paths.
          - {name: AWS_PROFILE, value: acme-ReadOnly}
          - {name: AWS_SHARED_CREDENTIALS_FILE, value: /home/sandbox/.aws/credentials}
          - {name: AWS_CONFIG_FILE, value: /home/sandbox/.aws/config}
```

## Async / idempotent

- Non-blocking: discovery runs in a background task off `aws/select`.
- Idempotent / relaunchable: each run republishes the kube dir's **contents in place**
  (rename desired files in, prune the rest) under the per-session lock — the dir inode
  stays stable for the live bind mount, and a re-select never leaves a half-written
  set. Re-selecting roles simply regenerates the kubeconfigs (and the refreshed
  `~/.aws`).
- Failures are logged (`eks_refresh_failed` / `eks_discover_skip`) and never crash
  the event loop or the `aws/select` request.

## Cleanup

The kube dir is a sibling of the `~/.aws` projection under the same session root, so
it is cleared wherever that root is:

- **Revoke** — `POST /connectors/{session_id}/aws/revoke` → `cleanup_aws_files(keep_dirs=True)`:
  clears the `.aws`/`kube` **contents in place** (and sweeps orphaned staging) but keeps
  the dirs, because the conv container may still bind-mount them — dropping the dir would
  detach the live mount, so the agent must see empty (revoked) creds through the same
  stable inode. Runs under the per-session lock so it can't interleave with a refresh.
- **Session end** — session delete → `cleanup_aws_files` (full `rmtree`, container gone)
  + `release_session_lock`.
- Credentials expire lazily from the RAM store; the background sweep keeps the store
  tidy. (Expired STS creds make `eks get-token` fail until the next re-select, which
  regenerates everything.)

## Scope notes

- Discovery is scoped to each profile's region (the SSO region). Clusters in other
  regions are not enumerated.
- Generating the kubeconfigs makes them **available** to the session. Wiring a
  discovered cluster into an orchestrator's `kubeconfig_path` (so the agent targets
  it automatically) is a separate concern — today kubectl tools still take an
  explicitly configured cluster; the agent reaches EKS read-only via `aws_call`.

## Configuration

No new settings. Reuses the AWS connector config (`AWS_SSO_*`, `AWS_SSO_ROLE_FILTER`)
and `AWS_CLI_PATH` (the `aws` binary used by the kubeconfig exec plugin).

## Code

- `backend/app/services/eks_refresh.py` — discovery, kubeconfig build, scheduler.
- `backend/app/tools/connector_exec.py` — `session_kube_dir`, tmpfs `~/.aws`
  projection, `cleanup_aws_files`.
- `backend/app/api/routes/connectors.py` — trigger in `aws_select`.
- `backend/app/api/routes/sessions.py` — tmpfs cleanup on session delete.
- `backend/tests/test_eks_refresh.py` — generation, idempotency, skip-on-no-access,
  cleanup, secret-free invariant.
