#!/usr/bin/env python3
"""Orchestration telemetry hook (UserPromptSubmit / Stop).

Renders a decorated status block for the most recent workflow run of this
project from its journal.jsonl. Never fails the turn: any error -> exit 0.
"""
import glob
import json
import os
import sys

PROJ = os.path.expanduser(
    "~/.claude/projects/-Users-jeremiah-Developer-claude-code-proxy"
)
W = 64


def latest_run():
    runs = glob.glob(f"{PROJ}/*/subagents/workflows/wf_*/journal.jsonl")
    return max(runs, key=os.path.getmtime) if runs else None


def load(journal):
    run_dir = os.path.dirname(journal)
    agents, order = {}, []
    with open(journal) as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            aid = ev.get("agentId")
            if ev.get("type") == "started" and aid:
                agents[aid] = {"label": ev.get("label", aid), "phase": ev.get("phase", "?"), "state": "run"}
                order.append(aid)
            elif ev.get("type") == "result" and aid in agents:
                agents[aid]["state"] = "done"
            elif ev.get("type") in ("error", "failed") and aid in agents:
                agents[aid]["state"] = "fail"
    for aid, a in agents.items():
        try:
            with open(os.path.join(run_dir, f"agent-{aid}.meta.json")) as fh:
                a["model"] = json.load(fh).get("model", "?")
        except (OSError, ValueError):
            a["model"] = "?"
    return os.path.basename(run_dir), [agents[a] for a in order]


def bar(pct, n=10):
    full = round(pct / 100 * n)
    return "█" * full + "░" * (n - full)


def render(run_id, agents):
    icon = {"done": "✓", "run": "◐", "fail": "✗"}
    done = sum(a["state"] == "done" for a in agents)
    pct = int(done / len(agents) * 100) if agents else 0
    phase = next((a["phase"] for a in reversed(agents) if a["state"] == "run"),
                 agents[-1]["phase"] if agents else "-")
    head = f" ORCH ─ {run_id} "
    out = [f"┌─{head}{'─' * (W - len(head) - 3)}┐",
           f"│ Phase: {phase:<16} [{bar(pct)}] {pct:>3}%  ({done}/{len(agents)})".ljust(W - 1) + "│"]
    for a in agents:
        out.append(f"│ {icon[a['state']]} {a['label']:<26} {a['model']:<7}".ljust(W - 1) + "│")
    out.append("└" + "─" * (W - 2) + "┘")
    return "\n".join(out)


def main():
    if "--demo" in sys.argv:
        print(render(*load(latest_run())))
        return
    try:
        json.load(sys.stdin)
    except ValueError:
        pass
    journal = latest_run()
    if journal:
        print(json.dumps({"systemMessage": render(*load(journal))}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
