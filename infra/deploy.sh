#!/usr/bin/env bash
# CCP model-group IaC driver.
#   infra/deploy.sh params                       write bicep/main.parameters.json from the registry
#   infra/deploy.sh init bicep|tf [--deploy-on-demand] [--include-legacy]
#   infra/deploy.sh env                          write endpoints into .env (no secrets, no keys)
# Env: AZ_SUBSCRIPTION (default: current az account), AZ_LOCATION (deployment metadata, default eastus)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
GROUPS_FILE="${CCP_GROUPS_FILE:-$ROOT/ccp_groups.json}"
[[ -f "$GROUPS_FILE" ]] || GROUPS_FILE="$HERE/ccp_groups.seed.json"
ENV_FILE="${CCP_ENV_FILE:-$ROOT/.env}"
DEPLOY_OD=false; INCLUDE_LEGACY=false

die() { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "missing dependency: $1"; }

params() {
  need python3
  python3 - "$GROUPS_FILE" "$HERE/bicep/main.parameters.json" "$DEPLOY_OD" "$INCLUDE_LEGACY" <<'PY'
import json, sys
src, dst, od, legacy = sys.argv[1:5]
groups = json.load(open(src))["groups"]
out = {"$schema": "https://schema.management.azure.com/schemas/2018-05-01/subscriptionDeploymentParameters.json#",
       "contentVersion": "1.0.0.0",
       "parameters": {"groups": {"value": groups},
                      "deployOnDemandModels": {"value": od == "true"},
                      "includeLegacy": {"value": legacy == "true"}}}
json.dump(out, open(dst, "w"), indent=2)
print(f"wrote {dst} ({len(groups)} groups from {src})")
PY
}

write_env() {  # args: JSON object {group_id: endpoint}
  need python3
  python3 - "$ENV_FILE" "$GROUPS_FILE" "$1" <<'PY'
import json, re, sys
env, reg, eps = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
ids = {g["id"] for g in json.load(open(reg))["groups"]}
lines = {}
try:
    for l in open(env):
        if "=" in l and not l.startswith("#"):
            k, v = l.rstrip("\n").split("=", 1); lines[k] = v
except FileNotFoundError:
    pass
for gid, ep in eps.items():
    if gid in ids:
        lines["CCP_ENDPOINT_" + re.sub(r"[^A-Z0-9]", "_", gid.upper())] = ep
open(env, "w").write("".join(f"{k}={v}\n" for k, v in lines.items()))
print(f"updated {env} with {len(eps)} endpoint(s)")
PY
}

init_bicep() {
  need az
  params
  local sub="${AZ_SUBSCRIPTION:-$(az account show --query id -o tsv)}"
  az account set --subscription "$sub"
  az deployment sub create --name "ccp-groups-$(date +%Y%m%d%H%M%S)" \
    --location "${AZ_LOCATION:-eastus}" \
    --template-file "$HERE/bicep/main.bicep" \
    --parameters "@$HERE/bicep/main.parameters.json" \
    --query 'properties.outputs.endpoints.value' -o json > "$HERE/.endpoints.json"
  write_env "$(python3 -c 'import json;print(json.dumps({e["id"]:e["endpoint"] for e in json.load(open("'"$HERE"'/.endpoints.json"))}))')"
}

init_tf() {
  need terraform
  local sub="${AZ_SUBSCRIPTION:-$(az account show --query id -o tsv)}"
  terraform -chdir="$HERE/terraform" init -input=false
  terraform -chdir="$HERE/terraform" apply -input=false \
    -var "subscription_id=$sub" -var "groups_file=$GROUPS_FILE" \
    -var "deploy_on_demand_models=$DEPLOY_OD" -var "include_legacy=$INCLUDE_LEGACY"
  write_env "$(terraform -chdir="$HERE/terraform" output -json endpoints)"
}

cmd="${1:-}"; shift || true
engine="${1:-}"; [[ "$cmd" == init ]] && shift || true
for a in "$@"; do case "$a" in
  --deploy-on-demand) DEPLOY_OD=true;; --include-legacy) INCLUDE_LEGACY=true;; *) die "unknown flag $a";; esac; done
case "$cmd:$engine" in
  params:*) params;;
  init:bicep) init_bicep;;
  init:tf|init:terraform) init_tf;;
  env:*) [[ -f "$HERE/.endpoints.json" ]] || die "no endpoints yet; run init first"; write_env "$(cat "$HERE/.endpoints.json")";;
  *) sed -n '2,6p' "${BASH_SOURCE[0]}"; exit 2;;
esac
