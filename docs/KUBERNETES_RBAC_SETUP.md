# Kubernetes RBAC Setup for Inkwest

Read-only access for inkwest. No secret access by design.

## 1. ServiceAccount

```bash
kubectl create namespace inkwest
kubectl create serviceaccount inkwest-reader -n inkwest
```

## 2. ClusterRole (read-only, no secrets)

```bash
kubectl apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: inkwest-reader
rules:
  - apiGroups: [""]
    resources:
      - pods
      - pods/log
      - services
      - endpoints
      - events
      - configmaps
      - namespaces
      - nodes
      - persistentvolumeclaims
      - persistentvolumes
      - replicationcontrollers
      - resourcequotas
      - limitranges
      - serviceaccounts
    verbs: ["get", "list", "watch"]

  - apiGroups: ["apps"]
    resources:
      - deployments
      - replicasets
      - statefulsets
      - daemonsets
    verbs: ["get", "list", "watch"]

  - apiGroups: ["batch"]
    resources:
      - jobs
      - cronjobs
    verbs: ["get", "list", "watch"]

  - apiGroups: ["networking.k8s.io"]
    resources:
      - ingresses
      - networkpolicies
    verbs: ["get", "list", "watch"]

  - apiGroups: ["policy"]
    resources:
      - poddisruptionbudgets
    verbs: ["get", "list", "watch"]

  - apiGroups: ["autoscaling"]
    resources:
      - horizontalpodautoscalers
    verbs: ["get", "list", "watch"]

  - apiGroups: ["storage.k8s.io"]
    resources:
      - storageclasses
    verbs: ["get", "list", "watch"]

  - apiGroups: ["metrics.k8s.io"]
    resources:
      - pods
      - nodes
    verbs: ["get", "list"]
EOF
```

## 3. ClusterRoleBinding

```bash
kubectl create clusterrolebinding inkwest-reader \
  --clusterrole=inkwest-reader \
  --serviceaccount=inkwest:inkwest-reader
```

## 4. Generate Kubeconfig (k8s 1.24+)

ServiceAccount tokens are no longer auto-created. Use the TokenRequest API:

```bash
# Get cluster info
CLUSTER_NAME="my-cluster"
API_SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA_DATA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

# Create a long-lived token (365 days)
TOKEN=$(kubectl create token inkwest-reader \
  -n inkwest \
  --duration=8760h)

# Generate kubeconfig
cat > inkwest-kubeconfig.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER_NAME}
    cluster:
      server: ${API_SERVER}
      certificate-authority-data: ${CA_DATA}
contexts:
  - name: inkwest@${CLUSTER_NAME}
    context:
      cluster: ${CLUSTER_NAME}
      user: inkwest-reader
      namespace: default
current-context: inkwest@${CLUSTER_NAME}
users:
  - name: inkwest-reader
    user:
      token: ${TOKEN}
EOF

echo "Kubeconfig written to inkwest-kubeconfig.yaml"
```

Place the generated file in the inkwest kubeconfig directory (default: `/etc/inkwest/kubeconfigs/`).

## Namespace-Scoped Alternative

To restrict access to specific namespaces, use `Role` + `RoleBinding` instead:

```bash
NAMESPACE="my-app"

kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: inkwest-reader
  namespace: ${NAMESPACE}
rules:
  - apiGroups: [""]
    resources: [pods, pods/log, services, endpoints, events, configmaps, persistentvolumeclaims, resourcequotas, limitranges, serviceaccounts]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: [deployments, replicasets, statefulsets, daemonsets]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: [jobs, cronjobs]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["networking.k8s.io"]
    resources: [ingresses, networkpolicies]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["metrics.k8s.io"]
    resources: [pods]
    verbs: ["get", "list"]
EOF

kubectl create rolebinding inkwest-reader \
  -n ${NAMESPACE} \
  --role=inkwest-reader \
  --serviceaccount=inkwest:inkwest-reader
```

Repeat for each namespace.

## Verify Permissions

```bash
# Should succeed
kubectl auth can-i get pods --as=system:serviceaccount:inkwest:inkwest-reader -A
kubectl auth can-i list deployments --as=system:serviceaccount:inkwest:inkwest-reader -A
kubectl auth can-i get pods/log --as=system:serviceaccount:inkwest:inkwest-reader -A
kubectl auth can-i list events --as=system:serviceaccount:inkwest:inkwest-reader -A
kubectl auth can-i list nodes --as=system:serviceaccount:inkwest:inkwest-reader

# Must fail
kubectl auth can-i get secrets --as=system:serviceaccount:inkwest:inkwest-reader -A
kubectl auth can-i create pods --as=system:serviceaccount:inkwest:inkwest-reader -A
kubectl auth can-i delete deployments --as=system:serviceaccount:inkwest:inkwest-reader -A
kubectl auth can-i exec pods --as=system:serviceaccount:inkwest:inkwest-reader -A
```

Expected output: `yes` for read operations, `no` for secrets and write operations.
