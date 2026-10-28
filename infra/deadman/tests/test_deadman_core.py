import os, sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deadman_core as dc

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
RG = "rg-ccp-chat-eus-od"


def acct(name, tags, rg=RG):
    return NS(name=name, id=f"/subscriptions/s/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{name}", tags=tags)


class Fake:
    def __init__(self, accounts, deployments):
        self.calls = []
        f = self
        self.accounts = NS(list=lambda: accounts, delete=lambda *a: f.calls.append(("account_delete", a)),
                           begin_delete=lambda *a: f.calls.append(("account_delete", a)))
        self.deployments = NS(list=lambda rg, a: deployments.get(a, []),
                              begin_delete=lambda rg, a, d: (f.calls.append(("dep_delete", a, d)), NS(result=lambda: None))[1])
        self.tags = NS(update_at_scope=lambda scope, body: f.calls.append(("tag", scope, body)))
        self.resource_groups = NS(begin_delete=lambda *a: f.calls.append(("rg_delete", a)))


def dep(name, created=None):
    return NS(name=name, system_data=NS(created_at=created, last_modified_at=None))


def run(accounts, deps, dry=False):
    f = Fake(accounts, deps)
    return dc.evaluate(f, f, NOW, dry), f


def test_only_on_demand_tagged_accounts_evaluated():
    old = (NOW - timedelta(hours=5)).isoformat()
    r, f = run([acct("always", {"ccp-policy": "always_on"}), acct("untagged", None)],
               {"always": [dep("gpt-x", NOW - timedelta(days=9))], "untagged": [dep("gpt-y", NOW - timedelta(days=9))]})
    assert r["accounts"] == [] and f.calls == []


def test_stale_heartbeat_deletes_only_deployment_and_tags_torn():
    tags = {"ccp-policy": "on_demand", "ccp-idle-minutes": "60",
            dc.heartbeat_tag("gpt-5.4-mini-ptu"): (NOW - timedelta(minutes=61)).isoformat(),
            dc.heartbeat_tag("gpt-fresh"): (NOW - timedelta(minutes=10)).isoformat()}
    r, f = run([acct("od", tags)], {"od": [dep("gpt-5.4-mini-ptu"), dep("gpt-fresh")]})
    assert [d["deployment"] for d in r["deleted"]] == ["gpt-5.4-mini-ptu"]
    assert [d["deployment"] for d in r["kept"]] == ["gpt-fresh"]
    kinds = [c[0] for c in f.calls]
    assert kinds == ["dep_delete", "tag"]
    assert "account_delete" not in kinds and "rg_delete" not in kinds
    body = f.calls[1][2]
    assert body["operation"] == "Merge" and list(body["properties"]["tags"]) == ["ccp-torn-gpt-5-4-mini-ptu"]


def test_missing_heartbeat_falls_back_to_system_data():
    r, f = run([acct("od", {"ccp-policy": "on_demand"})],
               {"od": [dep("new", NOW - timedelta(minutes=5)), dep("old", NOW - timedelta(hours=3))]})
    assert [d["deployment"] for d in r["deleted"]] == ["old"] and r["deleted"][0]["source"] == "systemData"


def test_dry_run_never_mutates():
    r, f = run([acct("od", {"ccp-policy": "on_demand"})], {"od": [dep("old", NOW - timedelta(hours=3))]}, dry=True)
    assert len(r["deleted"]) == 1 and f.calls == []
