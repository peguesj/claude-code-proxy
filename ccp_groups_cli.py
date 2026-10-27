#!/usr/bin/env python3
"""ccp_groups_cli.py - 'groups' and 'active' subcommands for ccp-litellm.

Talks to the CCP proxy HTTP API (default http://localhost:8082, override with
CCP_URL). When the proxy does not expose /ccp/* yet (HTTP 404) or is down,
'groups list', 'groups pick' and 'active' fall back to the local registry and
state files so they stay usable. Lifecycle actions (deploy/teardown/redeploy)
always require the server. Stdlib only.

Invoked by ~/.local/bin/ccp-litellm and the ccp wrapper; not meant to be run
standalone by end users, but works fine that way too:
    python3 ccp_groups_cli.py groups list -v
    python3 ccp_groups_cli.py groups pick chat-eus --action teardown
"""
from __future__ import annotations

import argparse
import curses
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("CCP_URL", "http://localhost:8082").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.environ.get("CCP_REGISTRY", os.path.join(HERE, "ccp_groups.json"))
STATE_PATH = os.environ.get("CCP_STATE", os.path.join(HERE, "apm", "ccp_group_state.json"))
ENV_PATH = os.path.join(HERE, ".env")

ACTIONS = ("deploy", "teardown", "redeploy")
KIND_ORDER = ("chat", "router", "embedding", "audio", "image", "other")
KIND_TITLES = {
    "chat": "Chat",
    "router": "Router",
    "embedding": "Embeddings",
    "audio": "Audio / Speech",
    "image": "Image",
    "other": "Other",
}
ENV_DEFAULTS = {"BIG_MODEL": "gpt-4.1", "SMALL_MODEL": "gpt-4.1-mini", "FRONTIER_MODEL": "gpt-5.2"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
class Unreachable(Exception):
    """The proxy did not answer at all (connection refused, timeout, ...)."""


def _parse_body(raw: bytes):
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:500]}


def api(method: str, path: str, body=None, timeout: float = 5.0):
    """Return (status, payload, headers). Raises Unreachable on transport errors."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_body(resp.read()), resp.headers
    except urllib.error.HTTPError as err:  # HTTPError is a URLError: must come first
        return err.code, _parse_body(err.read()), err.headers
    except (urllib.error.URLError, OSError) as err:
        raise Unreachable(str(getattr(err, "reason", err))) from err


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(note):
    if note:
        print(f"[warn] {note}", file=sys.stderr)


def model_not_deployed_hint(payload, headers=None):
    """Return a multi-line hint if payload is the 503 model_not_deployed error."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict) or err.get("type") != "model_not_deployed":
        return None
    group = err.get("group") or "<group>"
    model = err.get("model") or "<model>"
    lines = [
        "MODEL NOT DEPLOYED (HTTP 503)",
        f"  model:   {model}",
        f"  group:   {group}",
    ]
    if err.get("message"):
        lines.append(f"  detail:  {err['message']}")
    retry = headers.get("Retry-After") if headers is not None else None
    if retry:
        lines.append(f"  retry:   after {retry}s")
    lines.append(f"  fix:     ccp groups deploy {group} --models {model}")
    lines.append(f"           or: ccp groups pick {group}   (or the CCP menubar)")
    return "\n".join(lines)


def report_http_error(status: int, payload, headers, context: str):
    hint = model_not_deployed_hint(payload, headers)
    if hint:
        print(hint, file=sys.stderr)
        return
    detail = ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message", ""))
        else:
            detail = str(payload.get("detail") or payload.get("raw") or "")
    print(f"ERROR: {context} failed (HTTP {status}) {detail}".rstrip(), file=sys.stderr)


# ---------------------------------------------------------------------------
# Local files
# ---------------------------------------------------------------------------
def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except ValueError as err:
        die(f"invalid JSON in {path}: {err}")


def read_env() -> dict:
    values: dict = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
                if m:
                    values[m.group(1)] = m.group(2).strip("\"'")
    except FileNotFoundError:
        pass
    return values


def file_groups() -> list:
    """Registry merged with runtime state, used when the server is unavailable."""
    registry = load_json(REGISTRY_PATH, {"groups": []})
    state = load_json(STATE_PATH, {})
    merged = []
    for group in registry.get("groups", []):
        entry = dict(group)
        st = state.get(group.get("id", ""), {}) if isinstance(state, dict) else {}
        entry["status"] = st.get("status", "unknown")
        entry["last_used"] = st.get("last_used")
        entry["deployed_models"] = st.get("deployed_models", [])
        merged.append(entry)
    return merged


def fetch_groups():
    """Return (groups, note). note explains a fallback to local files, if any."""
    try:
        status, payload, headers = api("GET", "/ccp/groups")
    except Unreachable as err:
        return file_groups(), f"proxy unreachable at {BASE_URL} ({err}); showing local registry"
    if status == 200:
        groups = payload.get("groups", []) if isinstance(payload, dict) else payload
        if not isinstance(groups, list):
            die("unexpected payload from /ccp/groups (expected a list)")
        return groups, None
    if status == 404:
        return file_groups(), "server has no /ccp/groups yet (HTTP 404); showing local registry"
    report_http_error(status, payload, headers, "GET /ccp/groups")
    sys.exit(1)


def find_group(groups: list, gid: str) -> dict:
    for group in groups:
        if group.get("id") == gid:
            return group
    known = ", ".join(g.get("id", "?") for g in groups) or "(none)"
    die(f"unknown group '{gid}'. Known groups: {known}")


def deployed_set(group: dict) -> set:
    return set(group.get("deployed_models") or [])


def is_deployed(model: dict, deployed: set) -> bool:
    return model.get("name") in deployed or model.get("deployment") in deployed


def short_ts(value) -> str:
    return str(value).replace("T", " ")[:16] if value else "-"


def parse_models(raw: str) -> list:
    return [m.strip() for m in (raw or "").split(",") if m.strip()]


def validate_models(group: dict, models: list):
    known = [m.get("name") for m in group.get("models") or []]
    if not models or not known:
        return
    unknown = [m for m in models if m not in known]
    if unknown:
        die(f"unknown model(s) for group {group['id']}: {', '.join(unknown)}. "
            f"Known: {', '.join(known)}")


def print_table(rows: list):
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


# ---------------------------------------------------------------------------
# groups list / active
# ---------------------------------------------------------------------------
def cmd_list(args) -> int:
    groups, note = fetch_groups()
    warn(note)
    if not groups:
        print(f"No groups defined (registry: {REGISTRY_PATH})")
        return 0
    rows = [["GROUP", "RESOURCE GROUP", "REGION", "POLICY", "BILLING", "STATUS", "MODELS", "LAST USED"]]
    for g in groups:
        models = g.get("models") or []
        deployed = deployed_set(g)
        n_live = sum(1 for m in models if is_deployed(m, deployed))
        rows.append([
            g.get("id", "?"), g.get("resource_group", "?"), g.get("region", "?"),
            g.get("policy", "?"), g.get("billing", "?"), g.get("status", "unknown"),
            f"{n_live}/{len(models)}", short_ts(g.get("last_used")),
        ])
    print_table(rows)
    if args.verbose:
        for g in groups:
            deployed = deployed_set(g)
            print(f"\n{g.get('id', '?')}:")
            for m in g.get("models") or []:
                state = "deployed" if is_deployed(m, deployed) else "torn down"
                address = m.get("addressable") or m.get("name", "?")
                print(f"  - {address:<36} {m.get('kind', '?'):<10} "
                      f"{m.get('sku', '?')} x{m.get('capacity', '?'):<4} {state}")
            if g.get("reconciled_at"):
                print(f"  (reconciled {short_ts(g['reconciled_at'])})")
    return 0


def cmd_reconcile(_args) -> int:
    try:
        status, payload, headers = api("POST", "/ccp/reconcile?wait=true", timeout=180)
    except Unreachable as exc:
        die(f"proxy unreachable at {BASE_URL}: {exc}")
    if status != 200:
        report_http_error(status, payload, headers, "reconcile")
        return 1
    rows = [["GROUP", "STATUS", "DEPLOYED"]]
    for gid, info in sorted((payload or {}).get("groups", {}).items()):
        rows.append([gid, info.get("status", "?"), info.get("deployed", 0)])
    print_table(rows)
    return 0


def cmd_active(args) -> int:
    try:
        status, payload, headers = api("GET", "/ccp/active")
    except Unreachable as err:
        die(f"proxy unreachable at {BASE_URL} ({err}). Start it with: ccp-litellm start")
    if status == 404:
        env = read_env()
        warn("server has no /ccp/active yet (HTTP 404); showing .env model mapping")
        payload = {
            "big": env.get("BIG_MODEL", ENV_DEFAULTS["BIG_MODEL"]),
            "small": env.get("SMALL_MODEL", ENV_DEFAULTS["SMALL_MODEL"]),
            "frontier": env.get("FRONTIER_MODEL", ENV_DEFAULTS["FRONTIER_MODEL"]),
            "recent": [],
        }
    elif status != 200:
        report_http_error(status, payload, headers, "GET /ccp/active")
        return 1
    print(f"Frontier (opus):  {payload.get('frontier', '-')}")
    print(f"Big (sonnet):     {payload.get('big', '-')}")
    print(f"Small (haiku):    {payload.get('small', '-')}")
    recent = payload.get("recent") or []
    print("\nRecently used:")
    if not recent:
        print("  (none)")
    else:
        rows = [["MODEL", "GROUP", "LAST USED"]]
        for item in recent:
            rows.append([item.get("model", "?"), item.get("group", "-"), short_ts(item.get("last_used"))])
        print_table(rows)
    return 0


# ---------------------------------------------------------------------------
# groups deploy / teardown / redeploy
# ---------------------------------------------------------------------------
def post_action(gid: str, action: str, models: list) -> int:
    body = {"models": models} if models else {}
    try:
        status, payload, headers = api("POST", f"/ccp/groups/{gid}/{action}", body, timeout=15)
    except Unreachable as err:
        die(f"proxy unreachable at {BASE_URL} ({err}). Start it with: ccp-litellm start")
    if status == 404:
        die(f"server has no /ccp/groups/{gid}/{action} endpoint yet (HTTP 404); "
            "the lifecycle API is not available on this proxy build")
    if status in (200, 202):
        scope = ", ".join(models) if models else "all models"
        state = payload.get("status", "?") if isinstance(payload, dict) else "?"
        print(f"{action}: group {gid} ({scope}) accepted (HTTP {status}), status={state}")
        print("Lifecycle work runs in the background. Check progress with: ccp groups list")
        return 0
    report_http_error(status, payload, headers, f"{action} {gid}")
    return 1


def cmd_action(args, action: str) -> int:
    models = parse_models(args.models)
    groups, note = fetch_groups()
    warn(note)
    group = find_group(groups, args.group)
    validate_models(group, models)
    return post_action(args.group, action, models)


# ---------------------------------------------------------------------------
# groups pick (curses multi-select)
# ---------------------------------------------------------------------------
def build_rows(items: list, query: str) -> list:
    """Rows are ("h", kind, member_indexes) for section headers or ("m", index, None)."""
    q = query.strip().lower()
    kinds = list(KIND_ORDER) + sorted({it["kind"] for it in items} - set(KIND_ORDER))
    rows = []
    for kind in kinds:
        members = []
        for idx, it in enumerate(items):
            if it["kind"] != kind:
                continue
            haystack = f"{it['name']} {it['deployment']} {it['kind']} {it['sku']}".lower()
            if q and q not in haystack:
                continue
            members.append(idx)
        if members:
            rows.append(("h", kind, members))
            rows.extend(("m", idx, None) for idx in members)
    return rows


def _visible_names(rows: list, items: list) -> list:
    return [items[r[1]]["name"] for r in rows if r[0] == "m"]


def _toggle_row(rows: list, cursor: int, items: list, selected: set):
    if not rows:
        return
    tag, payload, members = rows[cursor]
    if tag == "m":
        name = items[payload]["name"]
        selected.symmetric_difference_update({name})
        return
    names = [items[i]["name"] for i in members]
    if all(n in selected for n in names):
        selected.difference_update(names)
    else:
        selected.update(names)


def _draw(stdscr, group: dict, items: list, rows: list, query: str, selected: set,
          action: str, cursor: int, top: int, body_h: int, msg: str):
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    def put(y, text, attr=0):
        if 0 <= y < height:
            try:
                stdscr.addnstr(y, 0, text, max(width - 1, 1), attr)
            except curses.error:
                pass

    shown = sum(1 for r in rows if r[0] == "m")
    put(0, f"Group {group.get('id')}  rg={group.get('resource_group', '?')}  "
           f"{group.get('region', '?')}  policy={group.get('policy', '?')}  "
           f"billing={group.get('billing', '?')}", curses.A_BOLD)
    put(1, f"Action: [{action}]  (Tab: change)     Selected: {len(selected)}/{len(items)}"
           f"     Shown: {shown}")
    put(2, f"Search: {query}_")
    put(3, "Type to search   Space: toggle (on a section header: whole section)")
    put(4, "Ctrl-A: select all shown   Ctrl-N: deselect all shown   Ctrl-U: clear search")
    put(5, "Enter: confirm   Esc: clear search (or cancel)   Up/Down PgUp/PgDn Home/End: move")
    put(6, "-" * max(width - 1, 1))

    for offset in range(body_h):
        row_index = top + offset
        if row_index >= len(rows):
            break
        tag, payload, members = rows[row_index]
        y = 7 + offset
        if tag == "h":
            total = len(members)
            chosen = sum(1 for i in members if items[i]["name"] in selected)
            text = f"-- {KIND_TITLES.get(payload, payload)} ({chosen}/{total}) --"
            attr = curses.A_BOLD
        else:
            it = items[payload]
            mark = "x" if it["name"] in selected else " "
            live = "  (deployed)" if it["deployed"] else ""
            text = (f"[{mark}] {it['name']:<28} {it['sku']:<16} x{str(it['capacity']):<5} "
                    f"{it['deployment']}{live}")
            attr = 0
        if row_index == cursor:
            attr |= curses.A_REVERSE
            text = ">" + text[1:] if tag == "m" else text
        put(y, text, attr)

    put(height - 1, msg, curses.A_BOLD)
    stdscr.refresh()


def run_picker(stdscr, group: dict, items: list, action: str):
    """Return (action, [model names]) on confirm, or None when cancelled."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    actions = list(ACTIONS)
    act_i = actions.index(action)
    selected: set = set()
    query = ""
    cursor = 0
    top = 0
    msg = ""

    while True:
        rows = build_rows(items, query)
        cursor = max(0, min(cursor, len(rows) - 1))
        height, _ = stdscr.getmaxyx()
        body_h = max(height - 8, 1)
        if cursor < top:
            top = cursor
        if cursor >= top + body_h:
            top = cursor - body_h + 1
        _draw(stdscr, group, items, rows, query, selected, actions[act_i], cursor, top, body_h, msg)
        msg = ""

        try:
            key = stdscr.get_wch()
        except curses.error:
            continue

        if isinstance(key, str):
            if key in ("\n", "\r"):
                if not selected:
                    msg = "Nothing selected. Use Space or Ctrl-A first."
                    continue
                chosen = [it["name"] for it in items if it["name"] in selected]
                return actions[act_i], chosen
            if key == "\t":
                act_i = (act_i + 1) % len(actions)
            elif key == " ":
                _toggle_row(rows, cursor, items, selected)
            elif key == "\x01":  # Ctrl-A
                selected.update(_visible_names(rows, items))
            elif key == "\x0e":  # Ctrl-N
                selected.difference_update(_visible_names(rows, items))
            elif key == "\x15":  # Ctrl-U
                query, cursor, top = "", 0, 0
            elif key == "\x1b":  # Esc
                if query:
                    query, cursor, top = "", 0, 0
                else:
                    return None
            elif key in ("\x7f", "\b"):
                query = query[:-1]
                cursor = 0
                top = 0
            elif key.isprintable() and key != " ":
                query += key
                cursor = 0
                top = 0
        else:
            if key == curses.KEY_UP:
                cursor = max(cursor - 1, 0)
            elif key == curses.KEY_DOWN:
                cursor = min(cursor + 1, len(rows) - 1)
            elif key == curses.KEY_PPAGE:
                cursor = max(cursor - body_h, 0)
            elif key == curses.KEY_NPAGE:
                cursor = min(cursor + body_h, len(rows) - 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(rows) - 1
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
                cursor = 0
                top = 0
            elif key in (curses.KEY_ENTER, 10, 13):
                if not selected:
                    msg = "Nothing selected. Use Space or Ctrl-A first."
                    continue
                chosen = [it["name"] for it in items if it["name"] in selected]
                return actions[act_i], chosen


def cmd_pick(args) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        die("'groups pick' is interactive and needs a terminal. "
            "Use 'groups deploy|teardown|redeploy <id> --models a,b' in scripts.")
    groups, note = fetch_groups()
    warn(note)
    group = find_group(groups, args.group)
    raw_models = group.get("models") or []
    if not raw_models:
        die(f"group {args.group} has no models in the registry")
    deployed = deployed_set(group)
    items = [{
        "name": m.get("name", "?"),
        "kind": m.get("kind") or "other",
        "deployment": m.get("deployment", m.get("name", "?")),
        "sku": m.get("sku", "?"),
        "capacity": m.get("capacity", "?"),
        "deployed": is_deployed(m, deployed),
    } for m in raw_models]

    os.environ.setdefault("ESCDELAY", "25")
    result = curses.wrapper(run_picker, group, items, args.action)
    if result is None:
        print("Cancelled.")
        return 1
    action, chosen = result
    print(f"{action} {len(chosen)} model(s) in {args.group}: {', '.join(chosen)}")
    answer = input("Proceed? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted.")
        return 1
    return post_action(args.group, action, chosen)


# ---------------------------------------------------------------------------
# annotate (used by 'ccp-litellm log' to flag 503 model_not_deployed lines)
# ---------------------------------------------------------------------------
def cmd_annotate(_args) -> int:
    for line in sys.stdin:
        sys.stdout.write(line)
        if "model_not_deployed" in line:
            group = re.search(r'"group"\s*:\s*"([^"]+)"', line)
            model = re.search(r'"model"\s*:\s*"([^"]+)"', line)
            if group and model:
                payload = {"error": {"type": "model_not_deployed",
                                     "group": group.group(1), "model": model.group(1)}}
                hint = model_not_deployed_hint(payload)
                if hint:
                    sys.stdout.write("    >> " + hint.replace("\n", "\n    >> ") + "\n")
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccp", description="CCP proxy groups and active models")
    sub = parser.add_subparsers(dest="cmd", required=True)

    groups = sub.add_parser("groups", help="list and manage on-demand model resource groups")
    gsub = groups.add_subparsers(dest="verb", required=True)

    gl = gsub.add_parser("list", help="list groups with status and deployed model counts")
    gl.add_argument("-v", "--verbose", action="store_true", help="also list models per group")
    gsub.add_parser("reconcile", help="refresh group state from Azure's actual deployments")

    for verb, desc in (("deploy", "deploy models (default: all) in a group"),
                       ("teardown", "tear down models (default: all) in a group"),
                       ("redeploy", "redeploy models (default: all) in a group")):
        gp = gsub.add_parser(verb, help=desc)
        gp.add_argument("group", help="group id, e.g. chat-eus")
        gp.add_argument("--models", default="", help="comma-separated model names")

    gpk = gsub.add_parser("pick", help="interactive multi-select of models to act on")
    gpk.add_argument("group", help="group id, e.g. chat-eus")
    gpk.add_argument("--action", choices=ACTIONS, default="deploy",
                     help="initial action (Tab cycles deploy/teardown/redeploy)")

    sub.add_parser("active", help="show big/small/frontier models and recent usage")
    sub.add_parser("annotate", help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "active":
        return cmd_active(args)
    if args.cmd == "annotate":
        return cmd_annotate(args)
    if args.verb == "list":
        return cmd_list(args)
    if args.verb == "reconcile":
        return cmd_reconcile(args)
    if args.verb == "pick":
        return cmd_pick(args)
    return cmd_action(args, args.verb)


if __name__ == "__main__":
    sys.exit(main())
