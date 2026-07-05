#!/usr/bin/env bash
# One-time Elasticsearch setup for the Raspberry Pi logging pipeline.
# Runs on the fedora server; talks to ES via the tailnet ingress.
set -euo pipefail

ES=https://elasticsearch.tail652475.ts.net
KB=https://kibana.tail652475.ts.net
PW=$(sudo k3s kubectl -n logging get secret logs-es-elastic-user -o jsonpath='{.data.elastic}' | base64 -d)
auth=(-u "elastic:${PW}")
json=(-H 'Content-Type: application/json')

echo "== 1. ILM policy pi-logs-14d =="
curl -sS "${auth[@]}" "${json[@]}" -X PUT "$ES/_ilm/policy/pi-logs-14d" -d '{
  "policy": {
    "phases": {
      "hot":    { "actions": { "rollover": { "max_age": "1d", "max_primary_shard_size": "10gb" } } },
      "delete": { "min_age": "14d", "actions": { "delete": {} } }
    }
  }
}'; echo

echo "== 2. Index template logs-pi (data stream) =="
TPL='{
  "index_patterns": ["logs-pi.*"],
  "data_stream": {},
  "priority": 500,
  "composed_of": ["logs@mappings", "logs@settings", "ecs@mappings"],
  "template": { "settings": { "index.lifecycle.name": "pi-logs-14d", "index.number_of_replicas": 0 } }
}'
out=$(curl -sS "${auth[@]}" "${json[@]}" -X PUT "$ES/_index_template/logs-pi" -d "$TPL")
echo "$out"
if echo "$out" | grep -q '"error"'; then
  echo "-- retrying without ecs@mappings --"
  curl -sS "${auth[@]}" "${json[@]}" -X PUT "$ES/_index_template/logs-pi" \
    -d "$(echo "$TPL" | sed 's/, "ecs@mappings"//')"; echo
fi

echo "== 3. Role filebeat-writer =="
curl -sS "${auth[@]}" "${json[@]}" -X PUT "$ES/_security/role/filebeat-writer" -d '{
  "cluster": ["monitor"],
  "indices": [{
    "names": ["logs-pi.*"],
    "privileges": ["create_doc", "auto_configure", "view_index_metadata"]
  }]
}'; echo

echo "== 4. User filebeat-pi =="
FBPW=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-28)
curl -sS "${auth[@]}" "${json[@]}" -X PUT "$ES/_security/user/filebeat-pi" \
  -d "{\"password\": \"${FBPW}\", \"roles\": [\"filebeat-writer\"]}"; echo

echo "== 5. Store filebeat creds as k8s secret (out-of-git convention) =="
sudo k3s kubectl -n logging delete secret filebeat-pi --ignore-not-found
sudo k3s kubectl -n logging create secret generic filebeat-pi \
  --from-literal=username=filebeat-pi --from-literal=password="${FBPW}"

echo "== 6. Smoke test: write as filebeat-pi =="
curl -sS -u "filebeat-pi:${FBPW}" "${json[@]}" \
  -X POST "$ES/logs-pi.journald-default/_doc?op_type=create" \
  -d "{\"@timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"message\": \"smoke test from es-setup\"}"; echo

echo "== 7. Verify data stream + ILM attachment =="
curl -sS "${auth[@]}" "$ES/_data_stream/logs-pi.journald-default" | head -c 600; echo

echo "== 8. Remove smoke-test data stream (recreated on first real write) =="
curl -sS "${auth[@]}" -X DELETE "$ES/_data_stream/logs-pi.journald-default"; echo

echo "== 9. Kibana data view logs-pi.* =="
curl -sS "${auth[@]}" "${json[@]}" -H 'kbn-xsrf: true' -X POST "$KB/api/data_views/data_view" -d '{
  "data_view": {
    "title": "logs-pi.*",
    "name": "Raspberry Pi logs",
    "timeFieldName": "@timestamp",
    "allowNoIndex": true
  }
}' | head -c 400; echo

echo "== DONE =="
