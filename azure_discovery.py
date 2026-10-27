"""Azure account and AI resource discovery helpers for CCP profiles."""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional


class AzureDiscoveryError(RuntimeError):
    pass


def run_az_json(args: List[str], timeout: int = 30) -> Any:
    cmd = ["az", *args, "-o", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AzureDiscoveryError("Azure CLI is not installed or not on PATH") from exc
    except subprocess.SubprocessError as exc:
        raise AzureDiscoveryError(str(exc)) from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "az command failed"
        raise AzureDiscoveryError(detail)

    import json

    return json.loads(result.stdout or "null")


def active_account() -> Dict[str, Any]:
    account = run_az_json(["account", "show"])
    return {
        "id": account.get("id"),
        "name": account.get("name"),
        "tenant_id": account.get("tenantId"),
        "user": (account.get("user") or {}).get("name"),
        "state": account.get("state"),
        "is_default": bool(account.get("isDefault")),
    }


def subscriptions() -> List[Dict[str, Any]]:
    rows = run_az_json(["account", "list"])
    return [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "tenant_id": row.get("tenantId"),
            "state": row.get("state"),
            "is_default": bool(row.get("isDefault")),
        }
        for row in rows or []
    ]


def cognitive_accounts(subscription_id: Optional[str] = None) -> List[Dict[str, Any]]:
    args = ["cognitiveservices", "account", "list"]
    if subscription_id:
        args.extend(["--subscription", subscription_id])
    rows = run_az_json(args)
    accounts = []
    for row in rows or []:
        properties = row.get("properties") or {}
        sku = row.get("sku") or {}
        accounts.append(
            {
                "name": row.get("name"),
                "resource_group": row.get("resourceGroup"),
                "location": row.get("location"),
                "kind": row.get("kind"),
                "sku": sku.get("name"),
                "endpoint": properties.get("endpoint"),
                "subscription_id": subscription_id,
            }
        )
    return accounts


def deployments(resource_group: str, account_name: str, subscription_id: Optional[str] = None) -> List[Dict[str, Any]]:
    args = [
        "cognitiveservices",
        "account",
        "deployment",
        "list",
        "--resource-group",
        resource_group,
        "--name",
        account_name,
    ]
    if subscription_id:
        args.extend(["--subscription", subscription_id])
    rows = run_az_json(args)
    found = []
    for row in rows or []:
        properties = row.get("properties") or {}
        model = properties.get("model") or {}
        sku = row.get("sku") or {}
        found.append(
            {
                "name": row.get("name"),
                "model": model.get("name"),
                "version": model.get("version"),
                "format": model.get("format"),
                "sku": sku.get("name"),
                "capacity": sku.get("capacity"),
                "provisioning_state": properties.get("provisioningState"),
            }
        )
    return found


def discovery(include_deployments: bool = False) -> Dict[str, Any]:
    active = active_account()
    subs = subscriptions()
    accounts_by_subscription = {}
    for subscription in subs:
        sub_id = subscription.get("id")
        if not sub_id:
            continue
        try:
            accounts = cognitive_accounts(sub_id)
        except AzureDiscoveryError as exc:
            accounts_by_subscription[sub_id] = {"error": str(exc), "accounts": []}
            continue
        if include_deployments:
            for account in accounts:
                rg = account.get("resource_group")
                name = account.get("name")
                if not rg or not name:
                    continue
                try:
                    account["deployments"] = deployments(rg, name, sub_id)
                except AzureDiscoveryError as exc:
                    account["deployments_error"] = str(exc)
        accounts_by_subscription[sub_id] = {"accounts": accounts}
    return {
        "active_account": active,
        "subscriptions": subs,
        "cognitive_accounts": accounts_by_subscription,
    }
