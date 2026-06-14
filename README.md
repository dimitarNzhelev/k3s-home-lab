# k3s-home-lab

GitOps source of truth for a single-node [k3s](https://k3s.io) home server.
[Argo CD](https://argo-cd.readthedocs.io) runs in the cluster, watches this
repository, and reconciles the cluster to match what's committed here. Change the
lab by committing YAML — not by running `kubectl`/`helm` by hand.

## What's deployed

| Component | Source | Purpose |
|---|---|---|
| Argo CD | `argo/argo-cd` | GitOps engine — self-manages from this repo |
| kube-prometheus-stack | `prometheus-community/kube-prometheus-stack` | Prometheus, Grafana, Alertmanager, node-exporter, kube-state-metrics |
| Tailscale Operator | `tailscale/tailscale-operator` | Publishes the UIs as `*.ts.net` HTTPS over the tailnet |
| AdGuard Home | `manifests/adguard` | Network-wide DNS ad/tracker blocking |
| blackbox-exporter | `prometheus-community/prometheus-blackbox-exporter` | HTTP/uptime probes |
| smartctl-exporter | `prometheus-community/prometheus-smartctl-exporter` | NVMe SMART health metrics |
| speedtest-exporter | `manifests/speedtest-exporter` | Hourly ISP speed metrics |
| monitoring-extras | `manifests/monitoring-extras` | PrometheusRules, blackbox Probes, Discord AlertmanagerConfig |
| grafana-dashboards | `dashboards/` (kustomize) | Dashboards auto-loaded into Grafana via the sidecar |

## Web UIs (tailnet HTTPS)

| UI | URL |
|---|---|
| Grafana | https://grafana.tail652475.ts.net |
| Argo CD | https://argocd.tail652475.ts.net |
| Prometheus | https://prometheus.tail652475.ts.net |
| Alertmanager | https://alertmanager.tail652475.ts.net |
| AdGuard Home | https://adguard.tail652475.ts.net |

## Layout

```
bootstrap/root-app.yaml     # app-of-apps root → points Argo CD at apps/
apps/                       # one Argo CD Application per workload
values/                     # Helm values for the chart-based apps
ingress/                    # tailscale Ingresses for the UIs
manifests/                  # raw manifests for the non-Helm apps
  adguard/  speedtest-exporter/  monitoring-extras/
dashboards/                 # Grafana dashboard JSON + kustomization (→ ConfigMaps)
```

Chart-based apps use **multi-source** Applications (upstream chart + values from
this repo). Raw-manifest apps point an Application at a `manifests/<app>` directory.

## Alerting

All Alertmanager alerts (except the always-firing Watchdog) notify **Discord**.

`manifests/monitoring-extras/discord-alertmanagerconfig.yaml` — an
`AlertmanagerConfig` using `slackConfigs.apiURL` pointed at Discord's
Slack-compatible `<webhook>/slack` endpoint. (The prometheus-operator's config
validator rejects native `discord_configs` `webhook_url_file`, hence the Slack
endpoint.) `alertmanagerConfigMatcherStrategy: OnNamespaceExceptForAlertmanagerNamespace`
(in `values/kube-prometheus-stack.yaml`) makes the monitoring-namespace config
apply to alerts from every namespace; the Watchdog is diverted to an empty receiver.

Custom alert rules: `HomeServerOnBattery` (mains power lost → running on battery),
`HomeServerBatteryCritical`, `SmartDiskHealthFailing`, `SmartDiskHot`.

## Secrets you must create (NEVER committed)

Kept out of Git; recreate by hand on a rebuild.

| Secret | Namespace | Keys | Used by |
|---|---|---|---|
| `grafana-admin` | `monitoring` | `admin-user`, `admin-password` | Grafana login |
| `operator-oauth` | `tailscale` | `client_id`, `client_secret` | Tailscale Operator |
| `alertmanager-discord` | `monitoring` | `url` (Discord webhook **+ `/slack`**) | Alertmanager → Discord |

```bash
kubectl create secret generic grafana-admin -n monitoring \
  --from-literal=admin-user=admin --from-literal=admin-password='<pw>'

kubectl create secret generic operator-oauth -n tailscale \
  --from-literal=client_id='<id>' --from-literal=client_secret='<secret>'

kubectl create secret generic alertmanager-discord -n monitoring \
  --from-literal=url='https://discord.com/api/webhooks/<id>/<token>/slack'
```

The **AdGuard** admin lives in its own PVC (not a k8s secret) and is recreated via
its first-run wizard.

## Bootstrap from scratch

```bash
kubectl create namespace argocd
kubectl create namespace monitoring
# create the grafana-admin secret (above)
helm repo add argo https://argoproj.github.io/argo-helm && helm repo update
helm install argo-cd argo/argo-cd -n argocd --version 9.5.21 -f values/argo-cd.yaml
kubectl apply -f bootstrap/root-app.yaml
```

Argo CD then syncs everything in `apps/`. Create the remaining secrets (above) for
the components that need them, and run the AdGuard first-run wizard.

## Accessing the UIs

Over the tailnet at the `*.ts.net` URLs above — HTTPS, gated by your Tailscale ACLs.

Port-forward fallback:
```bash
kubectl -n argocd port-forward svc/argo-cd-argocd-server 8080:80
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
```
Initial Argo CD admin password:
`kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d`

## Notes for k3s

`kubeControllerManager`, `kubeScheduler`, `kubeProxy`, and `kubeEtcd` scrape jobs
are disabled in `values/kube-prometheus-stack.yaml` — k3s bundles those into one
process and this is a single-node SQLite cluster (no etcd), so their ServiceMonitors
would sit permanently "down".

To use AdGuard as your DNS, set the tailnet nameserver to the `adguard-dns`
Tailscale device (`100.92.233.36`) in the Tailscale admin console.
