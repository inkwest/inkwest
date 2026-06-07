-- migrate:up

-- a session may start without a Kubernetes cluster. Kube access can
-- also arrive later in the session via AWS (EKS kubeconfigs), so a session is no
-- longer required to reference a cluster at creation time.
ALTER TABLE inkwest.sessions ALTER COLUMN cluster_id DROP NOT NULL;

-- migrate:down

-- Reverting requires every session to reference a cluster again. Detach any
-- cluster-less sessions first so the NOT NULL constraint can be re-applied.
DELETE FROM inkwest.sessions WHERE cluster_id IS NULL;
ALTER TABLE inkwest.sessions ALTER COLUMN cluster_id SET NOT NULL;
