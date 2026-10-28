"""Dead-man teardown for CCP idle-cost (on_demand) Azure AI deployments.

Only accounts tagged ccp-policy=on_demand are evaluated. A deployment is deleted when its
heartbeat tag (ccp-hb-<deployment>, written by the proxy on use/deploy) is older than
ccp-idle-minutes; with no heartbeat, the deployment's systemData timestamp is used.
Only the deployment is deleted: never the account, resource group, or tags. A
ccp-torn-<deployment> tag is added so the model stays identifiable for same-name redeploy.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("ccp-deadman")


def heartbeat_tag(deployment: str) -> str:  # must match ccp_lifecycle.heartbeat_tag
    return "ccp-hb-" + re.sub(r"[^A-Za-z0-9_-]", "-", deployment)[:240]


def torn_tag(deployment: str) -> str:
    return heartbeat_tag(deployment).replace("ccp-hb-", "ccp-torn-", 1)


def _parse(ts: Any) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rg_of(resource_id: str) -> str:
    return resource_id.split("/resourceGroups/")[1].split("/")[0]


def evaluate(cog, res, now: datetime, dry_run: bool) -> Dict[str, Any]:
    """cog: CognitiveServicesManagementClient, res: ResourceManagementClient (or fakes)."""
    report: Dict[str, Any] = {"now": now.isoformat(), "dry_run": dry_run, "accounts": [], "deleted": [], "kept": []}
    for acct in cog.accounts.list():
        tags = {k.lower(): v for k, v in (acct.tags or {}).items()}
        if tags.get("ccp-policy") != "on_demand":
            continue
        rg = _rg_of(acct.id)
        idle = int(tags.get("ccp-idle-minutes", "60") or 60)
        report["accounts"].append({"account": acct.name, "resource_group": rg, "idle_minutes": idle})
        torn: Dict[str, str] = {}
        for dep in cog.deployments.list(rg, acct.name):
            hb = _parse(tags.get(heartbeat_tag(dep.name).lower()))
            sd = getattr(dep, "system_data", None)
            fallback = _parse(getattr(sd, "last_modified_at", None)) or _parse(getattr(sd, "created_at", None))
            last = hb or fallback
            age = (now - last) if last else None
            row = {"account": acct.name, "deployment": dep.name,
                   "last_seen": last.isoformat() if last else None, "source": "heartbeat" if hb else "systemData"}
            if age is not None and age <= timedelta(minutes=idle):
                report["kept"].append(row)
                continue
            report["deleted"].append(row)
            log.warning("dead-man: %s %s/%s idle (%s)", "WOULD DELETE" if dry_run else "deleting",
                        acct.name, dep.name, row["last_seen"])
            if not dry_run:
                cog.deployments.begin_delete(rg, acct.name, dep.name).result()
                torn[torn_tag(dep.name)] = now.isoformat()
        if torn and not dry_run:
            # Merge (never replace) so heartbeat/policy tags survive.
            res.tags.update_at_scope(acct.id, {"operation": "Merge", "properties": {"tags": torn}})
    return report
