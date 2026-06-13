# k3s-home-lab

GitOps source of truth for a single-node [k3s](https://k3s.io) home server.
[Argo CD](https://argo-cd.readthedocs.io) runs in the cluster, watches this
repository, and reconciles the cluster to match what is committed here. Change
the infrastructure by committing YAML — not by running `kubectl`/`helm` by hand.

## What's deployed

| Component | Chart | Purpose |
|-----------|-------|---------|
| Argo CD | `argo/argo-cd` | GitOps engine — also manages itself from this repo |
| kube-prometheus-stack | `prometheus-community/kube-prometheus-stack` | Prometheus, Grafana, Alertmanager, node-exporter, kube-state-metrics |
| Tailscale Operator | `tailscale/tailscale-operator` | Publishes the UIs as `*.ts.net` HTTPS URLs over the tailnet |

UIs are published via `tailscale` Ingresses (`ingress/`):

| UI | URL |
|----|-----|
| Grafana | https://grafana.tail652475.ts.net |
| Argo CD | https://argocd.tail652475.ts.net |
| Prometheus | https://prometheus.tail652475.ts.net |
| Alertmanager | https://alertmanager.tail652475.ts.net |

## Layout

```
bootstrap/root-app.yaml          # App-of-apps root → points Argo CD at apps/
apps/                            # one Argo CD Application per workload
  argo-cd.yaml                   #   Argo CD self-management
  kube-prometheus-stack.yaml
values/                          # Helm values, pulled by the Applications above
  argo-cd.yaml
  kube-prometheus-stack.yaml
```

Each Application in `apps/` is a **multi-source** Argo CD Application: the chart
is pulled from its upstream Helm repo at a pinned version, and its `values` file
is pulled from *this* repo (`$values/values/<app>.yaml`). Upstream charts stay
upstream; only configuration lives here.

## Secrets you must create (NEVER committed)

Secrets are deliberately kept **out of Git**. They are created out of band and
must be recreated by hand if the cluster is rebuilt.

| Secret | Namespace | Keys | Used by |
|--------|-----------|------|---------|
| `grafana-admin` | `monitoring` | `admin-user`, `admin-password` | Grafana admin login |
| `operator-oauth` | `tailscale` | `client_id`, `client_secret` | Tailscale Operator (later phase) |

Create the Grafana admin secret:

```bash
kubectl create secret generic grafana-admin -n monitoring \
  --from-literal=admin-user=admin \
  --from-literal=admin-password='<your-password>'
```

## Bootstrap from scratch

```bash
# 1. Namespaces
kubectl create namespace argocd
kubectl create namespace monitoring

# 2. Grafana admin secret (see above)

# 3. Install Argo CD once, with the same values it will later self-manage
helm repo add argo https://argoproj.github.io/argo-helm && helm repo update
helm install argo-cd argo/argo-cd -n argocd --version 9.5.21 \
  -f values/argo-cd.yaml

# 4. Hand the cluster to Git
kubectl apply -f bootstrap/root-app.yaml
```

Argo CD then syncs everything in `apps/`.

## Accessing the UIs

Over the tailnet at the `*.ts.net` URLs above — HTTPS, gated by your Tailscale
ACLs, no exposed ports.

Fallback via port-forward (e.g. if the operator is unavailable):

```bash
kubectl -n argocd port-forward svc/argo-cd-argocd-server 8080:80              # http://localhost:8080
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80  # http://localhost:3000
```

Initial Argo CD admin password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d ; echo
```

## Notes for k3s

`kubeControllerManager`, `kubeScheduler`, `kubeProxy`, and `kubeEtcd` scrape jobs
are disabled in `values/kube-prometheus-stack.yaml` — k3s bundles those into one
process and this is a single-node SQLite cluster (no etcd), so their default
ServiceMonitors would sit permanently "down". Re-enable by exposing the relevant
k3s metrics bind addresses if you ever want those series.
