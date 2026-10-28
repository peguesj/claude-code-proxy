"""On-demand model group lifecycle for CCP.

Loads ccp_groups.json (hot reload on mtime), tracks last-used per model,
drives `az` to deploy/teardown/redeploy Azure AI Foundry deployments, reaps idle
on_demand deployments, and builds the 503 "model_not_deployed" response.

Semantics:
- Models not present in any group are never gated (existing routing is untouched).
- policy=always_on groups are never gated or reaped.
- An always_on group with no recorded state is listed as "unknown" (never gated) until
  reconcile() records what Azure actually has. An on_demand group with no recorded state
  is treated as torn down: by design its idle-cost deployments only exist after an
  explicit deploy (CLI / menubar), so requests get the 503 redeploy hint instead of
  an opaque upstream 404.
- Teardown deletes the deployments (the cost driver) and keeps the resource group
  and account, so redeploy does not hit soft-delete/custom-domain collisions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
REAPER_INTERVAL_SECONDS = 300
RETRY_AFTER_SECONDS = 120
LEGACY_GROUP_ID = "legacy-werc"
PROVIDER_PREFIXES = ("azure/", "azure_ai/", "openai/")
NON_GROUP_PREFIXES = ("anthropic/", "gemini/")

AzRunner = Callable[[List[str]], Awaitable[Tuple[int, str, str]]]
HEARTBEAT_THROTTLE_SECONDS = 300


DEPLOY_RETRY_DELAYS = (15, 30, 60, 120)  # deployment name busy (deleting/failed): wait, never rename
_BUSY_MARKERS = ("deleting", "inuse", "in use", "conflict", "provisioningstate", "another operation", "failed state")


def torn_tag(deployment: str) -> str:
    """Tag set when a deployment is torn down, so it stays identifiable by its registry name."""
    return heartbeat_tag(deployment).replace("ccp-hb-", "ccp-torn-", 1)


def heartbeat_tag(deployment: str) -> str:
    """Azure tag name holding a deployment's last-used time; must match infra/deadman."""
    import re
    return "ccp-hb-" + re.sub(r"[^A-Za-z0-9_-]", "-", deployment)[:240]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def default_az_runner(args: List[str]) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "az", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class GroupManager:
    def __init__(
        self,
        groups_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
        az_runner: Optional[AzRunner] = None,
        clock: Callable[[], datetime] = _now,
        cred_fetcher: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
    ) -> None:
        self._cred_fetcher = cred_fetcher or self._az_fetch_credential
        self._cred_cache: Dict[str, Optional[str]] = {}
        self.groups_path = Path(groups_path or os.environ.get("CCP_GROUPS_FILE") or BASE_DIR / "ccp_groups.json")
        self.state_path = Path(state_path or os.environ.get("CCP_GROUP_STATE_FILE") or BASE_DIR / "apm" / "ccp_group_state.json")
        self.az: AzRunner = az_runner or default_az_runner
        self.clock = clock
        self._groups: Dict[str, Dict[str, Any]] = {}
        self._mtime: Optional[float] = None
        self._state: Optional[Dict[str, Any]] = None
        self._locks: Dict[str, asyncio.Lock] = {}
        self._tasks: set = set()
        self._reaper: Optional[asyncio.Task] = None
        self._inflight: set = set()
        self._last_touch_save = 0.0
        self._hb_last: Dict[str, float] = {}
        self.retry_delays = DEPLOY_RETRY_DELAYS
        self.sleep = asyncio.sleep

    # ---- registry -------------------------------------------------------
    def groups(self) -> Dict[str, Dict[str, Any]]:
        try:
            mtime = self.groups_path.stat().st_mtime_ns
        except OSError:
            self._groups, self._mtime = {}, None
            return self._groups
        if mtime != self._mtime:
            try:
                data = json.loads(self.groups_path.read_text())
                self._groups = {g["id"]: g for g in data.get("groups", []) if isinstance(g, dict) and g.get("id")}
                self._mtime = mtime
            except (OSError, ValueError, KeyError) as exc:
                logger.error("ccp_groups.json reload failed, keeping previous registry: %s", exc)
        return self._groups

    @staticmethod
    def is_legacy(group: Dict[str, Any]) -> bool:
        return group.get("id") == LEGACY_GROUP_ID or bool(group.get("legacy"))

    def _ordered_groups(self) -> List[Dict[str, Any]]:
        # Plain names: legacy first (existing routing wins), then always_on, then on_demand.
        rank = lambda g: (0 if self.is_legacy(g) else 1 if g.get("policy") != "on_demand" else 2)
        return sorted(self.groups().values(), key=rank)

    def resolve(self, model_name: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """Resolve a requested model to (group, model).

        Accepts `<group_id>/<model>` (explicit, reaches on_demand copies), a unique deployment
        name such as `gpt-5.4-ptu`, or a plain name (legacy > always_on > on_demand).
        anthropic/ and gemini/ prefixed names are never resolved to Azure groups.
        """
        name = (model_name or "").strip()
        if name.startswith(NON_GROUP_PREFIXES):
            return None
        while name.startswith(PROVIDER_PREFIXES):
            name = name.split("/", 1)[1]
        if not name:
            return None
        groups = self.groups()
        if "/" in name:
            gid, rest = name.split("/", 1)
            group = groups.get(gid)
            if group is None:
                name = name.split("/")[-1]
            else:
                for model in group.get("models", []):
                    if rest in (model.get("name"), model.get("deployment")):
                        return group, model
                return None
        ordered = self._ordered_groups()
        for group in ordered:
            for model in group.get("models", []):
                if model.get("deployment") == name and model.get("deployment") != model.get("name"):
                    return group, model
        for group in ordered:
            for model in group.get("models", []):
                if name in (model.get("name"), model.get("deployment")):
                    return group, model
        return None

    def find_model(self, model_name: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        return self.resolve(model_name)

    @staticmethod
    def addressable_name(group: Dict[str, Any], model: Dict[str, Any], collides: bool) -> str:
        return f"{group['id']}/{model['name']}" if collides else model["name"]

    def name_collisions(self) -> Dict[str, List[str]]:
        seen: Dict[str, List[str]] = {}
        for group in self.groups().values():
            for model in group.get("models", []):
                seen.setdefault(model["name"], []).append(group["id"])
        return {n: gids for n, gids in seen.items() if len(gids) > 1}

    # ---- per-group credentials / routing --------------------------------
    @staticmethod
    def _env_id(group_id: str) -> str:
        return group_id.upper().replace("-", "_")

    def _endpoint(self, group: Dict[str, Any]) -> str:
        eid = self._env_id(group["id"])
        for var in (f"AZURE_GROUP_{eid}_ENDPOINT", f"CCP_ENDPOINT_{eid}"):
            if os.environ.get(var):
                return os.environ[var].rstrip("/")
        return f"https://{group['account']}.cognitiveservices.azure.com"

    def _credential(self, group: Dict[str, Any]) -> Optional[str]:
        gid = group["id"]
        env_val = os.environ.get(f"AZURE_GROUP_{self._env_id(gid)}_API_KEY")
        if env_val:
            return env_val
        if gid in self._cred_cache:
            return self._cred_cache[gid]
        value = self._cred_fetcher(group)
        self._cred_cache[gid] = value  # memory only; never persisted or logged
        return value

    @staticmethod
    def _az_fetch_credential(group: Dict[str, Any]) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["az", "cognitiveservices", "account", "keys", "list", "-g", group["resource_group"],
                 "-n", group["account"], "--query", "key1", "-o", "tsv"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = proc.stdout.strip()
        return out if proc.returncode == 0 and out else None

    def route(self, model_name: str, api_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """LiteLLM routing params for a model in a non-legacy group, or None to keep existing routing."""
        found = self.resolve(model_name)
        if not found:
            return None
        group, model = found
        if self.is_legacy(group):
            return None
        deployment = model.get("deployment") or model["name"]
        endpoint = self._endpoint(group)
        params: Dict[str, Any] = {"api_key": self._credential(group)}
        fmt = (model.get("format") or "OpenAI").lower()
        if fmt == "openai":
            params.update(model=f"azure/{deployment}", api_base=endpoint)
            if api_version:
                params["api_version"] = api_version
        else:
            # Foundry partner models (Meta, Mistral, DeepSeek, xAI, OSS...) use the model-inference endpoint.
            host = f"https://{group['account']}.services.ai.azure.com"
            params.update(model=f"azure_ai/{deployment}", api_base=f"{host}/models", custom_llm_provider="azure_ai")
            if api_version:
                params["api_version"] = api_version
        params["ccp_group"] = group["id"]
        return params

    # ---- state ----------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        if self._state is None:
            try:
                self._state = json.loads(self.state_path.read_text())
            except (OSError, ValueError):
                self._state = {}
        return self._state

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".ccp_state.")
            with os.fdopen(fd, "w") as fh:
                json.dump(self.state(), fh, indent=2, sort_keys=True)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.error("could not persist ccp group state: %s", exc)

    def _default_status(self, group_id: str) -> str:
        group = self.groups().get(group_id) or {}
        # Never claim "deployed" without evidence; reconcile() replaces this with Azure's truth.
        return "torn_down" if group.get("policy") == "on_demand" else "unknown"

    def _entry(self, group_id: str) -> Dict[str, Any]:
        return self.state().setdefault(group_id, {"status": self._default_status(group_id),
                                                  "last_used": _iso(self.clock()), "deployed_models": []})

    def touch(self, model_name: str) -> None:
        found = self.find_model(model_name)
        if not found:
            return
        group, model = found
        entry = self._entry(group["id"])
        now = _iso(self.clock())
        entry["last_used"] = now
        entry.setdefault("model_last_used", {})[model["name"]] = now
        # Throttle disk writes on the request hot path; the reaper reads in-memory state.
        mono = time.monotonic()
        if mono - self._last_touch_save >= 15:
            self._last_touch_save = mono
            self._save()
        if group.get("policy") == "on_demand":
            self._schedule_heartbeat(group, [model.get("deployment", model["name"])])

    # ---- Azure dead-man heartbeat ----------------------------------------
    def _schedule_heartbeat(self, group: Dict[str, Any], deployments: List[str]) -> None:
        acct = group["account"]
        mono = time.monotonic()
        if mono - self._hb_last.get(acct, -1e9) < HEARTBEAT_THROTTLE_SECONDS:
            return
        self._hb_last[acct] = mono
        try:
            task = asyncio.get_running_loop().create_task(self.push_heartbeat(group, deployments))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def push_heartbeat(self, group: Dict[str, Any], deployments: List[str]) -> bool:
        """Tag the on_demand account so the Azure dead-man function sees recent use. Never raises."""
        now = _iso(self.clock())
        tags = [f"ccp-policy=on_demand", f"ccp-idle-minutes={int(group.get('idle_teardown_minutes', 60))}"]
        tags += [f"{heartbeat_tag(d)}={now}" for d in deployments]
        return await self._tag(group, tags)

    async def _tag(self, group: Dict[str, Any], tags: List[str]) -> bool:
        try:
            code, _, err = await self.az([
                "resource", "tag", "--is-incremental", "-g", group["resource_group"], "-n", group["account"],
                "--resource-type", "Microsoft.CognitiveServices/accounts", "--tags", *tags, "-o", "none"])
        except Exception as exc:  # noqa: BLE001
            code, err = 1, str(exc)
        if code != 0:
            logger.warning("dead-man heartbeat for %s failed: %s", group["account"], err.strip()[:200])
            return False
        return True

    def is_available(self, group: Dict[str, Any], model: Dict[str, Any]) -> bool:
        if group.get("policy") != "on_demand":
            return True
        entry = self.state().get(group["id"])
        if entry is None:
            return False  # on_demand + never deployed => torn down
        if entry.get("status") != "deployed":
            return False
        deployed = entry.get("deployed_models")
        return not deployed or model["name"] in deployed

    def not_deployed_response(self, model_name: str) -> Optional[JSONResponse]:
        """503 response if the model sits in a torn-down on_demand group; None if routable."""
        found = self.find_model(model_name)
        if not found:
            return None
        group, model = found
        if self.is_available(group, model):
            self.touch(model_name)
            return None
        gid, name = group["id"], model["name"]
        idle = group.get("idle_teardown_minutes", 60)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            content={"error": {
                "type": "model_not_deployed",
                "message": (f"Model {name} in group {gid} is torn down (idle >{idle}m). "
                            f"Redeploy via 'ccp groups deploy {gid} --models {name}' or the CCP menubar."),
                "group": gid,
                "model": name,
            }},
        )

    # ---- views ----------------------------------------------------------
    def list_groups(self) -> List[Dict[str, Any]]:
        out = []
        for gid, group in self.groups().items():
            entry = self.state().get(gid) or {}
            status = entry.get("status", self._default_status(gid))
            deployed = list(entry.get("deployed_models") or [])
            if status == "deployed" and not deployed and not entry.get("reconciled_at"):
                # empty list on a deployed group means "all models" internally; expose it explicitly
                deployed = sorted({m["name"] for m in group.get("models", []) if m.get("name")})
            st = {"status": status, "last_used": entry.get("last_used"), "deployed_models": deployed,
                  "torn_down_models": list(entry.get("torn_down_models") or [])}
            if entry.get("error"):
                st["error"] = entry["error"]
            if entry.get("reconciled_at"):
                st["reconciled_at"] = entry["reconciled_at"]
            collisions = self.name_collisions()
            group = {**group, "models": [
                {**m, "addressable": self.addressable_name(group, m, m["name"] in collisions
                                                           and self.resolve(m["name"]) != (group, m))}
                for m in group.get("models", [])]}
            # contract: groups merged with state -> top-level fields; "state" kept for older clients
            out.append({**group, **st, "state": dict(st)})
        return out

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = []
        for gid, entry in self.state().items():
            for model, ts in (entry.get("model_last_used") or {}).items():
                rows.append({"model": model, "group": gid, "last_used": ts})
        rows.sort(key=lambda r: r["last_used"], reverse=True)
        return rows[:limit]

    # ---- lifecycle ------------------------------------------------------
    def _lock(self, gid: str) -> asyncio.Lock:
        return self._locks.setdefault(gid, asyncio.Lock())

    def busy(self, gid: str) -> bool:
        return gid in self._inflight or self._lock(gid).locked()

    def _select(self, group: Dict[str, Any], names: Optional[List[str]]) -> List[Dict[str, Any]]:
        models = group.get("models", [])
        if not names:
            return list(models)
        wanted = set(names)
        return [m for m in models if m["name"] in wanted or m.get("deployment") in wanted]

    def unknown_models(self, group: Dict[str, Any], names: Optional[List[str]]) -> List[str]:
        known = {m["name"] for m in group.get("models", [])} | {m.get("deployment") for m in group.get("models", [])}
        return [n for n in (names or []) if n not in known]

    def schedule(self, op: str, gid: str, names: Optional[List[str]]) -> None:
        """Mark the group in-flight and run the op as a background task."""
        entry = self._entry(gid)
        entry["status"] = "tearing_down" if op == "teardown" else "deploying"
        self._save()
        self._inflight.add(gid)
        task = asyncio.get_running_loop().create_task(self.run(op, gid, names))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, op: str, gid: str, names: Optional[List[str]]) -> None:
        group = self.groups().get(gid)
        if not group:
            self._inflight.discard(gid)
            return
        async with self._lock(gid):
          try:
            try:
                if op in ("teardown", "redeploy"):
                    await self._teardown(group, names)
                if op in ("deploy", "redeploy"):
                    await self._deploy(group, names)
            except Exception as exc:  # noqa: BLE001 - surface any failure as state
                logger.error("group %s %s failed: %s", gid, op, exc)
                self._entry(gid)["status"] = "error"
                self._entry(gid)["error"] = str(exc)[:500]
                self._save()
          finally:
            self._inflight.discard(gid)

    async def _az(self, args: List[str], ok_codes: Tuple[int, ...] = (0,)) -> str:
        code, out, err = await self.az(args)
        if code not in ok_codes:
            raise RuntimeError(f"az {' '.join(args[:3])} failed ({code}): {err.strip()[:300]}")
        return out

    async def _deploy(self, group: Dict[str, Any], names: Optional[List[str]]) -> None:
        rg, acct, region = group["resource_group"], group["account"], group["region"]
        models = self._select(group, names)
        await self._az(["group", "create", "-n", rg, "-l", region, "-o", "none"])
        code, _, _ = await self.az(["cognitiveservices", "account", "show", "-n", acct, "-g", rg, "-o", "none"])
        if code != 0:
            await self._az(["cognitiveservices", "account", "create", "-n", acct, "-g", rg, "-l", region,
                            "--kind", "AIServices", "--sku", "S0", "--custom-domain", acct, "--yes", "-o", "none"])
        for m in models:
            await self._create_deployment(rg, acct, m)
        entry = self._entry(group["id"])
        deployed = set(entry.get("deployed_models") or []) if names else set()
        deployed |= {m["name"] for m in models}
        now = _iso(self.clock())
        entry.update(status="deployed", last_used=now, deployed_models=sorted(deployed))
        entry["torn_down_models"] = sorted(set(entry.get("torn_down_models") or []) - deployed)
        entry.pop("error", None)
        for m in models:
            entry.setdefault("model_last_used", {})[m["name"]] = now
        self._save()
        if group.get("policy") == "on_demand" and models:
            # Fresh deploys get a heartbeat immediately so the Azure dead-man switch does not reap them.
            self._hb_last[acct] = time.monotonic()
            await self.push_heartbeat(group, [m.get("deployment", m["name"]) for m in models])

    @staticmethod
    def deployment_args(rg: str, acct: str, m: Dict[str, Any]) -> List[str]:
        """Registry is the source of truth: identical name/model/version/sku/capacity on every (re)deploy."""
        return ["cognitiveservices", "account", "deployment", "create", "-g", rg, "-n", acct,
                "--deployment-name", m.get("deployment", m["name"]), "--model-name", m["name"],
                "--model-version", str(m.get("version", "")), "--model-format", m.get("format", "OpenAI"),
                "--sku-name", m.get("sku", "GlobalStandard"), "--sku-capacity", str(m.get("capacity", 1)),
                "-o", "none"]

    async def _create_deployment(self, rg: str, acct: str, m: Dict[str, Any]) -> None:
        args = self.deployment_args(rg, acct, m)
        for delay in (*self.retry_delays, None):
            code, _, err = await self.az(args)
            if code == 0:
                return
            busy = any(k in err.lower() for k in _BUSY_MARKERS)
            if delay is None or not busy:
                raise RuntimeError(f"az deployment create {args[8]} failed ({code}): {err.strip()[:300]}")
            logger.info("deployment %s busy, retrying same name in %ss", args[8], delay)
            await self.sleep(delay)

    async def _teardown(self, group: Dict[str, Any], names: Optional[List[str]]) -> None:
        rg, acct = group["resource_group"], group["account"]
        models = self._select(group, names)
        for m in models:
            # exit code 3 style "not found" is tolerated: already gone is the goal state
            code, _, err = await self.az([
                "cognitiveservices", "account", "deployment", "delete", "-g", rg, "-n", acct,
                "--deployment-name", m.get("deployment", m["name"])])
            if code != 0 and "notfound" not in err.lower().replace(" ", "") and "not found" not in err.lower():
                raise RuntimeError(f"az deployment delete failed ({code}): {err.strip()[:300]}")
        entry = self._entry(group["id"])
        current = entry.get("deployed_models") or []
        if not current and group.get("policy") != "on_demand":
            # always_on entries created by touch() keep [] meaning "all models"; on_demand lists are explicit
            current = [m["name"] for m in group.get("models", [])]
        remaining = [n for n in current if n not in {m["name"] for m in models}]
        entry["deployed_models"] = remaining
        entry["status"] = "deployed" if remaining else "torn_down"
        if group.get("policy") == "on_demand":
            torn = set(entry.get("torn_down_models") or []) | {m["name"] for m in models}
            entry["torn_down_models"] = sorted(torn - set(remaining))
        self._save()
        if group.get("policy") == "on_demand" and models:
            await self._tag(group, [f"{torn_tag(m.get('deployment', m['name']))}={_iso(self.clock())}" for m in models])

    # ---- reconcile ------------------------------------------------------
    async def reconcile_group(self, gid: str) -> Optional[Dict[str, Any]]:
        group = self.groups().get(gid)
        if not group or self.busy(gid):
            return None
        code, out, err = await self.az([
            "cognitiveservices", "account", "deployment", "list",
            "-g", group["resource_group"], "-n", group["account"], "-o", "json"])
        if code != 0:
            low = err.lower()
            if "notfound" not in low.replace(" ", "") and "not found" not in low and "could not be found" not in low:
                entry = self._entry(gid)
                entry["error"] = f"reconcile failed: {err.strip()[:300]}"
                return entry
            listed: List[Dict[str, Any]] = []
            account_exists = False
        else:
            try:
                listed = json.loads(out or "[]")
            except ValueError:
                listed = []
            account_exists = True
        by_dep = {m.get("deployment") or m["name"]: m["name"] for m in group.get("models", [])}
        names = sorted({by_dep.get(d.get("name"), d.get("name")) for d in listed if d.get("name")})
        torn: List[str] = []
        if account_exists and group.get("policy") == "on_demand":
            c2, tout, _ = await self.az(["cognitiveservices", "account", "show", "-g", group["resource_group"],
                                         "-n", group["account"], "--query", "tags", "-o", "json"])
            try:
                acct_tags = json.loads(tout or "{}") if c2 == 0 else {}
            except ValueError:
                acct_tags = {}
            keys = {k.lower() for k in acct_tags} if isinstance(acct_tags, dict) else set()
            torn = sorted(m["name"] for m in group.get("models", [])
                          if torn_tag(m.get("deployment", m["name"])).lower() in keys and m["name"] not in names)
        entry = self._entry(gid)
        entry["torn_down_models"] = torn
        entry.update(
            status="deployed" if names else ("torn_down" if group.get("policy") == "on_demand" else "undeployed"),
            deployed_models=names,
            account_exists=account_exists,
            reconciled_at=_iso(self.clock()),
        )
        entry.pop("error", None)
        return entry

    async def reconcile(self) -> Dict[str, Any]:
        """Replace recorded state with what Azure actually has deployed, per group account."""
        result = {}
        for gid in list(self.groups()):
            try:
                entry = await self.reconcile_group(gid)
            except Exception as exc:  # noqa: BLE001
                logger.error("reconcile %s failed: %s", gid, exc)
                continue
            if entry is not None:
                result[gid] = {"status": entry.get("status"), "deployed": len(entry.get("deployed_models") or [])}
        self._save()
        return result

    def schedule_reconcile(self) -> None:
        task = asyncio.get_running_loop().create_task(self.reconcile())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- reaper ---------------------------------------------------------
    async def reap_once(self) -> List[Tuple[str, List[str]]]:
        reaped = []
        now = self.clock()
        for gid, group in self.groups().items():
            if group.get("policy") != "on_demand":
                continue
            entry = self.state().get(gid)
            if not entry or entry.get("status") != "deployed" or self.busy(gid):
                continue
            limit = group.get("idle_teardown_minutes", 60) * 60
            deployed = entry.get("deployed_models") or [m["name"] for m in group.get("models", [])]
            idle = []
            for name in deployed:
                ts = _parse((entry.get("model_last_used") or {}).get(name)) or _parse(entry.get("last_used"))
                if ts is None or (now - ts).total_seconds() > limit:
                    idle.append(name)
            if idle:
                entry["status"] = "tearing_down"
                self._save()
                await self.run("teardown", gid, idle)
                reaped.append((gid, idle))
        return reaped

    async def _reaper_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("idle reaper iteration failed: %s", exc)

    def start_reaper(self, interval: float = REAPER_INTERVAL_SECONDS) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reaper_loop(interval))

    def stop_reaper(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None


manager = GroupManager()
