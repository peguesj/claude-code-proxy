import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from unittest.mock import patch

os.environ["CCP_REAPER"] = "0"
os.environ["CCP_RECONCILE"] = "0"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Same isolated import as test_model_upgrade: never load the user's dotenv file,
# and keep server's import-time config identical regardless of test-file order.
with patch.dict(os.environ, {
    "PREFERRED_PROVIDER": "azure",
    "AZURE_API_KEY": "test-key",
    "AZURE_API_BASE": "https://azure.test",
    "BIG_MODEL": "gpt-5.6-terra",
    "SMALL_MODEL": "gpt-5.6-luna",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "CCP_REAPER": "0",
    "CCP_RECONCILE": "0",
}, clear=True), patch("dotenv.load_dotenv"):
    import server

from fastapi.testclient import TestClient

import ccp_lifecycle

GROUPS = {"version": 1, "groups": [
    {"id": "legacy-werc", "resource_group": "rg-legacy-aoai", "region": "eastus", "account": "werc", "purpose": "chat",
     "billing": "pay_per_token", "policy": "always_on", "idle_teardown_minutes": 60,
     "models": [{"name": "gpt-4o", "deployment": "gpt-4o"}]},
    {"id": "speech-eus", "resource_group": "rg-ccp-speech-eus", "region": "eastus", "account": "ccp-speech",
     "purpose": "speech", "billing": "idle_cost", "policy": "on_demand", "idle_teardown_minutes": 60,
     "models": [{"name": "gpt-audio", "deployment": "gpt-audio", "version": "1", "sku": "GlobalStandard",
                 "capacity": 1, "format": "OpenAI", "kind": "audio"},
                {"name": "gpt-audio-mini", "deployment": "gpt-audio-mini", "version": "1", "sku": "GlobalStandard",
                 "capacity": 1, "format": "OpenAI", "kind": "audio"}]},
]}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    gp, sp = tmp_path / "ccp_groups.json", tmp_path / "state.json"
    gp.write_text(json.dumps(GROUPS))
    calls = []

    async def fake_az(args):
        calls.append(args)
        return 0, "", ""

    clock = Clock()
    mgr = ccp_lifecycle.GroupManager(gp, sp, fake_az, clock)
    monkeypatch.setattr(ccp_lifecycle, "manager", mgr)
    return mgr, calls, clock, gp, sp


def set_state(mgr, status, last_used, deployed=None):
    mgr.state()["speech-eus"] = {"status": status, "last_used": last_used.isoformat(), "deployed_models": deployed or []}


def test_503_when_torn_down_anthropic_and_openai(env, monkeypatch):
    mgr, _, clock, *_ = env
    set_state(mgr, "torn_down", clock.now)
    monkeypatch.setattr(server.litellm, "completion", lambda **k: pytest.fail("provider must not be called"))
    c = TestClient(server.app)
    r = c.post("/v1/chat/completions", json={"model": "azure/gpt-audio", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503
    assert r.headers["retry-after"]
    err = r.json()["error"]
    assert err["type"] == "model_not_deployed" and err["group"] == "speech-eus" and err["model"] == "gpt-audio"
    assert "ccp groups deploy speech-eus --models gpt-audio" in err["message"]
    r = c.post("/v1/messages", json={"model": "azure/gpt-audio", "max_tokens": 5,
                                     "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and r.json()["error"]["type"] == "model_not_deployed"


def test_always_on_and_unregistered_not_gated(env):
    mgr, *_ = env
    assert mgr.not_deployed_response("azure/gpt-4o") is None
    assert mgr.not_deployed_response("something-else") is None


def test_deployed_model_touches_last_used(env):
    mgr, _, clock, *_ = env
    set_state(mgr, "deployed", clock.now, ["gpt-audio"])
    assert mgr.not_deployed_response("gpt-audio") is None
    assert mgr.not_deployed_response("gpt-audio-mini") is not None  # not in deployed_models
    assert mgr.recent()[0]["model"] == "gpt-audio"


def test_registry_hot_reload(env):
    mgr, _, _, gp, _ = env
    assert "new" not in mgr.groups()
    data = json.loads(gp.read_text())
    data["groups"].append({**data["groups"][1], "id": "new"})
    gp.write_text(json.dumps(data))
    os.utime(gp, ns=(1, 2_000_000_000_000_000_000))
    assert "new" in mgr.groups()
    gp.write_text("{broken")
    os.utime(gp, ns=(1, 3_000_000_000_000_000_000))
    assert "new" in mgr.groups()  # bad reload keeps previous registry


def test_reaper_timing(env):
    mgr, calls, clock, *_ = env
    set_state(mgr, "deployed", clock.now, ["gpt-audio", "gpt-audio-mini"])
    clock.now += timedelta(minutes=59)
    assert asyncio.run(mgr.reap_once()) == []
    assert calls == []
    clock.now += timedelta(minutes=2)
    assert asyncio.run(mgr.reap_once()) == [("speech-eus", ["gpt-audio", "gpt-audio-mini"])]
    assert sum(1 for a in calls if "delete" in a) == 2
    assert mgr.state()["speech-eus"]["status"] == "torn_down"
    assert "legacy-werc" not in mgr.state()


def test_reaper_keeps_recently_used_model(env):
    mgr, calls, clock, *_ = env
    set_state(mgr, "deployed", clock.now, ["gpt-audio", "gpt-audio-mini"])
    clock.now += timedelta(minutes=90)
    mgr.state()["speech-eus"]["model_last_used"] = {"gpt-audio": clock.now.isoformat()}
    assert asyncio.run(mgr.reap_once()) == [("speech-eus", ["gpt-audio-mini"])]
    assert mgr.state()["speech-eus"]["status"] == "deployed"
    assert mgr.state()["speech-eus"]["deployed_models"] == ["gpt-audio"]


def test_endpoints_return_202_and_run_az(env):
    mgr, calls, *_ = env
    with TestClient(server.app) as c:
        assert c.get("/ccp/health").json() == {"ok": True}
        r = c.post("/ccp/groups/speech-eus/deploy", json={"models": ["gpt-audio"]})
        assert r.status_code == 202 and r.json() == {"group": "speech-eus", "status": "deploying"}
        assert c.post("/ccp/groups/nope/deploy").status_code == 404
        assert c.post("/ccp/groups/speech-eus/deploy", json={"models": ["bogus"]}).status_code == 400
        for _ in range(100):
            if mgr.state()["speech-eus"]["status"] == "deployed":
                break
            time.sleep(0.02)
        assert mgr.state()["speech-eus"]["deployed_models"] == ["gpt-audio"]
        assert any(a[:4] == ["cognitiveservices", "account", "deployment", "create"] for a in calls)
        assert c.post("/ccp/groups/speech-eus/teardown").status_code == 202
        assert c.post("/ccp/groups/speech-eus/redeploy", json={}).status_code in (202, 409)
        listing = c.get("/ccp/groups").json()
        assert {g["id"] for g in listing} >= {"legacy-werc", "speech-eus"}
        active = c.get("/ccp/active").json()
        assert set(active) == {"big", "small", "frontier", "recent"}


def test_az_failure_sets_error_state(env):
    mgr, _, *_ = env

    async def boom(args):
        return 1, "", "quota exceeded"

    mgr.az = boom
    asyncio.run(mgr.run("deploy", "speech-eus", None))
    assert mgr.state()["speech-eus"]["status"] == "error"
    assert "quota" in mgr.state()["speech-eus"]["error"]


def test_never_deployed_on_demand_is_gated_and_listed_torn_down(env):
    mgr, *_ = env
    r = mgr.not_deployed_response("gpt-audio")
    assert r is not None and r.status_code == 503
    by_id = {g["id"]: g for g in mgr.list_groups()}
    assert by_id["speech-eus"]["status"] == "torn_down" and by_id["speech-eus"]["deployed_models"] == []
    # always_on with no recorded state is "unknown" until reconciled, never claimed deployed
    assert by_id["legacy-werc"]["status"] == "unknown"
    assert by_id["legacy-werc"]["deployed_models"] == []
    assert by_id["legacy-werc"]["state"]["status"] == "unknown"


def test_partial_teardown_of_torn_down_group_stays_torn_down(env):
    mgr, *_ = env
    asyncio.run(mgr.run("teardown", "speech-eus", ["gpt-audio"]))
    assert mgr.state()["speech-eus"]["status"] == "torn_down"
    assert mgr.state()["speech-eus"]["deployed_models"] == []


def test_busy_is_set_immediately_on_schedule(env):
    mgr, *_ = env

    async def go():
        mgr.schedule("deploy", "speech-eus", None)
        assert mgr.busy("speech-eus")
        await asyncio.gather(*list(mgr._tasks))
        assert not mgr.busy("speech-eus")
        assert mgr.state()["speech-eus"]["status"] == "deployed"

    asyncio.run(go())


def test_empty_model_list_rejected(env):
    with TestClient(server.app) as c:
        assert c.post("/ccp/groups/speech-eus/deploy", json={"models": []}).status_code == 400


# ---- per-group routing, reconcile, name collisions ------------------------
ROUTED = {"version": 1, "groups": [
    {"id": "legacy-werc", "resource_group": "rg-legacy-aoai", "region": "eastus2", "account": "werc", "policy": "always_on",
     "models": [{"name": "gpt-4o", "deployment": "gpt-4o", "format": "OpenAI"}]},
    {"id": "chat-eus", "resource_group": "rg-ccp-chat-eus", "region": "eastus", "account": "example-ccp-chat-eus-aoai",
     "policy": "always_on", "models": [
        {"name": "gpt-5.4-mini", "deployment": "gpt-5.4-mini", "format": "OpenAI"},
        {"name": "Llama-3.3-70B-Instruct", "deployment": "Llama-3.3-70B-Instruct", "format": "Meta"}]},
    {"id": "chat-eus-od", "resource_group": "rg-ccp-chat-eus-od", "region": "eastus", "account": "example-ccp-chat-eus-od-aoai",
     "policy": "on_demand", "idle_teardown_minutes": 60, "models": [
        {"name": "gpt-5.4-mini", "deployment": "gpt-5.4-mini-ptu", "format": "OpenAI",
         "sku": "GlobalProvisionedManaged", "capacity": 15}]},
]}


@pytest.fixture
def routed(tmp_path, monkeypatch):
    gp, sp = tmp_path / "groups.json", tmp_path / "state.json"
    gp.write_text(json.dumps(ROUTED))
    fetched = []

    def fetch(group):
        fetched.append(group["id"])
        return f"fetched-{group['id']}"

    az_out = {}

    async def fake_az(args):
        acct = args[args.index("-n") + 1] if "-n" in args else ""
        return az_out.get(acct, (0, "[]", ""))

    mgr = ccp_lifecycle.GroupManager(gp, sp, fake_az, Clock(), cred_fetcher=fetch)
    monkeypatch.setattr(ccp_lifecycle, "manager", mgr)
    for var in [v for v in os.environ if v.startswith(("AZURE_GROUP_", "CCP_ENDPOINT_"))]:
        monkeypatch.delenv(var)
    return mgr, fetched, az_out


def test_resolve_precedence_and_explicit_group(routed):
    mgr, *_ = routed
    assert mgr.resolve("gpt-5.4-mini")[0]["id"] == "chat-eus"          # always_on wins plain name
    assert mgr.resolve("azure/gpt-5.4-mini")[0]["id"] == "chat-eus"
    assert mgr.resolve("chat-eus-od/gpt-5.4-mini")[0]["id"] == "chat-eus-od"
    assert mgr.resolve("gpt-5.4-mini-ptu")[0]["id"] == "chat-eus-od"   # unique deployment name
    assert mgr.resolve("gpt-4o")[0]["id"] == "legacy-werc"
    assert mgr.resolve("anthropic/gpt-5.4-mini") is None
    assert mgr.resolve("chat-eus-od/nope") is None


def test_route_uses_group_account_and_env_credentials(routed, monkeypatch):
    mgr, fetched, _ = routed
    assert mgr.route("gpt-4o") is None  # legacy keeps existing AZURE_DEPLOYMENT_MAP routing
    monkeypatch.setenv("AZURE_GROUP_CHAT_EUS_ENDPOINT", "https://chat.example/")
    monkeypatch.setenv("AZURE_GROUP_CHAT_EUS_API_KEY", "env-cred")
    r = mgr.route("gpt-5.4-mini", "2025-04-01-preview")
    assert r == {"model": "azure/gpt-5.4-mini", "api_base": "https://chat.example", "api_key": "env-cred",
                 "api_version": "2025-04-01-preview", "ccp_group": "chat-eus"}
    p = mgr.route("Llama-3.3-70B-Instruct")
    assert p["model"] == "azure_ai/Llama-3.3-70B-Instruct" and p["custom_llm_provider"] == "azure_ai"
    assert p["api_base"] == "https://example-ccp-chat-eus-aoai.services.ai.azure.com/models"
    assert fetched == []


def test_route_fetches_credential_once_when_env_missing(routed):
    mgr, fetched, _ = routed
    a = mgr.route("chat-eus-od/gpt-5.4-mini")
    b = mgr.route("gpt-5.4-mini-ptu")
    assert a["model"] == b["model"] == "azure/gpt-5.4-mini-ptu"
    assert a["api_base"] == "https://example-ccp-chat-eus-od-aoai.cognitiveservices.azure.com"
    assert a["api_key"] == "fetched-chat-eus-od" and fetched == ["chat-eus-od"]


def test_openai_route_sends_group_model_to_its_account(routed, monkeypatch):
    mgr, *_ = routed
    seen = {}

    def fake_completion(**kw):
        seen.update(kw)
        return {"id": "x", "object": "chat.completion", "choices": []}

    monkeypatch.setattr(server.litellm, "completion", fake_completion)
    c = TestClient(server.app)
    r = c.post("/v1/chat/completions", json={"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert seen["model"] == "azure/gpt-5.4-mini"
    assert seen["api_base"] == "https://example-ccp-chat-eus-aoai.cognitiveservices.azure.com"
    assert seen["api_key"] == "fetched-chat-eus"
    # explicit on_demand copy that is torn down -> 503, provider never called
    seen.clear()
    r = c.post("/v1/chat/completions", json={"model": "chat-eus-od/gpt-5.4-mini",
                                            "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and r.json()["error"]["group"] == "chat-eus-od" and not seen


def test_reconcile_writes_real_deployments(routed):
    mgr, _, az_out = routed
    az_out["example-ccp-chat-eus-aoai"] = (0, json.dumps([{"name": "gpt-5.4-mini"}]), "")
    az_out["example-ccp-chat-eus-od-aoai"] = (3, "", "ERROR: (ResourceNotFound) The Resource could not be found.")
    az_out["werc"] = (0, json.dumps([{"name": "gpt-4o"}]), "")
    # before reconcile: no evidence -> unknown, not deployed
    assert {g["id"]: g["status"] for g in mgr.list_groups()}["chat-eus"] == "unknown"
    res = asyncio.run(mgr.reconcile())
    assert res["chat-eus"] == {"status": "deployed", "deployed": 1}
    assert res["chat-eus-od"] == {"status": "torn_down", "deployed": 0}
    by_id = {g["id"]: g for g in mgr.list_groups()}
    assert by_id["chat-eus"]["deployed_models"] == ["gpt-5.4-mini"]
    assert by_id["chat-eus-od"]["status"] == "torn_down"
    assert mgr.state()["chat-eus-od"]["account_exists"] is False
    assert json.loads(mgr.state_path.read_text())["chat-eus"]["reconciled_at"]


def test_reconcile_maps_deployment_names_and_empty_account(routed):
    mgr, _, az_out = routed
    az_out["example-ccp-chat-eus-od-aoai"] = (0, json.dumps([{"name": "gpt-5.4-mini-ptu"}]), "")
    az_out["example-ccp-chat-eus-aoai"] = (0, "[]", "")
    asyncio.run(mgr.reconcile())
    assert mgr.state()["chat-eus-od"]["deployed_models"] == ["gpt-5.4-mini"]
    by_id = {g["id"]: g for g in mgr.list_groups()}
    assert by_id["chat-eus"]["status"] == "undeployed" and by_id["chat-eus"]["deployed_models"] == []


def test_listing_exposes_addressable_name_for_collisions(routed):
    mgr, *_ = routed
    by_id = {g["id"]: g for g in mgr.list_groups()}
    assert by_id["chat-eus"]["models"][0]["addressable"] == "gpt-5.4-mini"
    assert by_id["chat-eus-od"]["models"][0]["addressable"] == "chat-eus-od/gpt-5.4-mini"


def test_reconcile_endpoint(routed):
    with TestClient(server.app) as c:
        r = c.post("/ccp/reconcile?wait=true")
        assert r.status_code == 200 and set(r.json()["groups"]) == {"legacy-werc", "chat-eus", "chat-eus-od"}
        assert c.post("/ccp/reconcile").status_code == 202


# ---- Azure dead-man heartbeat ----------------------------------------------
def _tag_calls(calls):
    return [c for c in calls if c[:2] == ["resource", "tag"]]


def test_heartbeat_tag_sanitizes():
    assert ccp_lifecycle.heartbeat_tag("gpt-5.4-mini-ptu") == "ccp-hb-gpt-5-4-mini-ptu"


def test_deploy_on_demand_pushes_heartbeat_immediately(env):
    mgr, calls, clock, _, _ = env
    asyncio.run(mgr.run("deploy", "speech-eus", ["gpt-audio"]))
    tags = _tag_calls(calls)
    assert len(tags) == 1
    args = tags[0]
    assert "ccp-speech" in args and "--is-incremental" in args
    assert "ccp-policy=on_demand" in args and "ccp-idle-minutes=60" in args
    assert f"ccp-hb-gpt-audio={clock.now.isoformat()}" in args


def test_touch_heartbeat_throttled_and_only_on_demand(env):
    mgr, calls, clock, _, _ = env
    set_state(mgr, "deployed", clock.now, ["gpt-audio"])

    async def go():
        mgr.touch("gpt-audio")
        mgr.touch("gpt-audio")  # within throttle window
        mgr.touch("gpt-4o")     # always_on: never heartbeats
        await asyncio.gather(*list(mgr._tasks))
    asyncio.run(go())
    assert len(_tag_calls(calls)) == 1


def test_heartbeat_failure_is_swallowed(env):
    mgr, _, _, _, _ = env

    async def boom(args):
        raise OSError("az missing")
    mgr.az = boom
    assert asyncio.run(mgr.push_heartbeat(mgr.groups()["speech-eus"], ["gpt-audio"])) is False


# ---- identity-preserving teardown / redeploy --------------------------------
def _create_calls(calls):
    return [c for c in calls if c[:4] == ["cognitiveservices", "account", "deployment", "create"]]


def _flag(args, name):
    return args[args.index(name) + 1]


def test_teardown_redeploy_roundtrip_uses_identical_registry_args(env):
    mgr, calls, _, _, _ = env
    group = mgr.groups()["speech-eus"]
    asyncio.run(mgr.run("deploy", "speech-eus", None))
    first = {_flag(c, "--deployment-name"): c for c in _create_calls(calls)}
    asyncio.run(mgr.run("teardown", "speech-eus", None))
    calls.clear()
    asyncio.run(mgr.run("deploy", "speech-eus", None))
    second = {_flag(c, "--deployment-name"): c for c in _create_calls(calls)}
    assert set(first) == set(second) == {m["deployment"] for m in group["models"]}
    for m in group["models"]:
        a, b = first[m["deployment"]], second[m["deployment"]]
        assert a == b
        assert _flag(b, "--model-version") == m["version"] and _flag(b, "--sku-name") == m["sku"]
        assert _flag(b, "--sku-capacity") == str(m["capacity"])


def test_teardown_tags_torn_and_never_deletes_account(env):
    mgr, calls, _, _, _ = env
    asyncio.run(mgr.run("teardown", "speech-eus", ["gpt-audio"]))
    assert not [c for c in calls if c[:3] == ["cognitiveservices", "account", "delete"] or c[:2] == ["group", "delete"]]
    tag_args = [a for c in _tag_calls(calls) for a in c]
    assert any(a.startswith("ccp-torn-gpt-audio=") for a in tag_args)
    assert mgr.state()["speech-eus"]["torn_down_models"] == ["gpt-audio"]


def test_busy_deployment_name_is_retried_not_renamed(env):
    mgr, calls, _, _, _ = env
    attempts = []

    async def flaky(args):
        calls.append(args)
        if args[:4] == ["cognitiveservices", "account", "deployment", "create"]:
            attempts.append(_flag(args, "--deployment-name"))
            if len(attempts) < 3:
                return 1, "", "ERROR: deployment is in Deleting state"
        return 0, "", ""

    async def nosleep(_):
        return None
    mgr.az, mgr.sleep = flaky, nosleep
    asyncio.run(mgr.run("deploy", "speech-eus", ["gpt-audio"]))
    assert attempts == ["gpt-audio"] * 3
    assert mgr.state()["speech-eus"]["status"] == "deployed"


def test_reconcile_marks_torn_tagged_models_torn_down(env):
    mgr, calls, _, _, _ = env

    async def az(args):
        if args[:4] == ["cognitiveservices", "account", "deployment", "list"]:
            return 0, json.dumps([{"name": "gpt-audio-mini"}]), ""
        if args[:3] == ["cognitiveservices", "account", "show"]:
            return 0, json.dumps({"ccp-torn-gpt-audio": "2026-01-01T00:00:00+00:00", "ccp-hb-gpt-audio": "x"}), ""
        return 0, "", ""
    mgr.az = az
    asyncio.run(mgr.reconcile_group("speech-eus"))
    row = {g["id"]: g for g in mgr.list_groups()}["speech-eus"]
    assert row["torn_down_models"] == ["gpt-audio"] and row["deployed_models"] == ["gpt-audio-mini"]
