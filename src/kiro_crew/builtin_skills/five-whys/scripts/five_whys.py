#!/usr/bin/env python3
"""five_whys.py — deterministic mechanical core for the five-whys research mode.

The event log (append-only JSONL) is the single source of truth. This script owns
the mechanics that must NOT drift: appending validated events, allocating node ids,
referential integrity, and folding the log into projections (tree / frontier /
current / report). All judgment (what to ask, whether an answer is a real cause,
when a thread is done) stays with the skill/LLM.

Stdlib only, Python 3.8+. No network, no credentials, never rewrites history.

Usage:
  five_whys.py ask     <log> --parent ID|root --stage S --q "..." [--origin proposed|user]
  five_whys.py answer  <log> --id ID --a "..." [--source ai|user|<cite>]
  five_whys.py focus   <log> --id ID
  five_whys.py done    <log> --id ID [--kind root|takeaway] [--note "..."]
  five_whys.py discuss <log> --anchor ID --text "..."
  five_whys.py prune   <log> --id ID [--reason "..."]
  five_whys.py event   <log> --json '{"type":"search",...}'   # plugin custom types
  five_whys.py view    <log>                                  # current path + open-branch count
  five_whys.py tree    <log>
  five_whys.py frontier<log>
  five_whys.py report  <log> [--title "..."]                  # folds to markdown
  five_whys.py validate<log>                                  # schema+integrity gate (exit 0/1)
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CORE_TYPES = {"ask", "answer", "focus", "done", "discuss", "prune"}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path):
    """Return list of event dicts. Missing file => empty log."""
    p = Path(path)
    if not p.exists():
        return []
    events = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit("corrupt log at line %d: %s" % (i, e))
        events.append(ev)
    return events


def _append(path, ev):
    """Append one event as a single JSON line. Append-only: never rewrites."""
    ev.setdefault("ts", _now())
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ---- fold helpers -----------------------------------------------------------

def _asks(events):
    """id -> ask event (first definition wins; duplicates are integrity errors)."""
    out: dict = {}
    for e in events:
        if e.get("type") == "ask":
            out.setdefault(e["id"], e)
    return out


def _pruned(events, asks):
    """Set of pruned ids plus all their descendants."""
    roots = {e["id"] for e in events if e.get("type") == "prune"}
    pruned = set()
    for nid in asks:
        cur = nid
        hit = False
        while cur is not None:
            if cur in roots:
                hit = True
                break
            cur = asks.get(cur, {}).get("parent")
        if hit:
            pruned.add(nid)
    return pruned


def _last(events, etype, key="id"):
    val = None
    for e in events:
        if e.get("type") == etype:
            val = e.get(key)
    return val


def _sort_key(nid):
    return tuple(int(x) for x in nid.split("."))


def _alloc_id(asks, parent):
    """Next child id under parent (None => root). Counts pruned ids to avoid reuse."""
    sibs = [i for i, a in asks.items() if a.get("parent") == parent]
    nxt = 1 + max((_sort_key(i)[-1] for i in sibs), default=0)
    return str(nxt) if parent is None else "%s.%s" % (parent, nxt)


def _answers(events):
    out: dict = {}
    for e in events:
        if e.get("type") == "answer":
            out[e["id"]] = e.get("a", "")
    return out


def _dones(events):
    out: dict = {}
    for e in events:
        if e.get("type") == "done":
            out[e["id"]] = e
    return out


def _children(asks, pruned, parent):
    kids = [i for i, a in asks.items() if a.get("parent") == parent and i not in pruned]
    return sorted(kids, key=_sort_key)


# ---- commands ---------------------------------------------------------------

def cmd_ask(a):
    events = _load(a.log)
    asks = _asks(events)
    parent = None if a.parent in (None, "", "root", "null") else a.parent
    if parent is not None and parent not in asks:
        raise SystemExit("parent %r does not exist" % parent)
    nid = _alloc_id(asks, parent)
    _append(a.log, {"type": "ask", "id": nid, "parent": parent,
                    "stage": a.stage, "q": a.q, "origin": a.origin})
    print(nid)


def _require(a, asks):
    if a.id not in asks:
        raise SystemExit("node %r does not exist" % a.id)


def cmd_answer(a):
    asks = _asks(_load(a.log))
    _require(a, asks)
    _append(a.log, {"type": "answer", "id": a.id, "a": a.a, "source": a.source})
    print("ok")


def cmd_focus(a):
    asks = _asks(_load(a.log))
    _require(a, asks)
    _append(a.log, {"type": "focus", "id": a.id})
    print("ok")


def cmd_done(a):
    asks = _asks(_load(a.log))
    _require(a, asks)
    _append(a.log, {"type": "done", "id": a.id, "kind": a.kind, "note": a.note})
    print("ok")


def cmd_discuss(a):
    asks = _asks(_load(a.log))
    if a.anchor not in asks:
        raise SystemExit("anchor %r does not exist" % a.anchor)
    _append(a.log, {"type": "discuss", "anchor": a.anchor, "text": a.text})
    print("ok")


def cmd_prune(a):
    asks = _asks(_load(a.log))
    _require(a, asks)
    _append(a.log, {"type": "prune", "id": a.id, "reason": a.reason})
    print("ok")


def cmd_event(a):
    try:
        ev = json.loads(a.json)
    except json.JSONDecodeError as e:
        raise SystemExit("invalid --json: %s" % e)
    if not isinstance(ev, dict) or "type" not in ev:
        raise SystemExit("event must be a JSON object with a 'type' field")
    _append(a.log, ev)
    print("ok")


def cmd_tree(a):
    events = _load(a.log)
    asks = _asks(events)
    pruned = _pruned(events, asks)
    answers = _answers(events)
    dones = _dones(events)
    cur = _last(events, "focus")
    lines = []

    def walk(nid, depth):
        node = asks[nid]
        mark = " ✅" if nid in dones else (" ⏳" if nid == cur else "")
        ans = answers.get(nid)
        tail = " → %s" % ans if ans else ""
        lines.append("%s- [%s] (%s) %s%s%s" % (
            "  " * depth, nid, node.get("stage", ""), node.get("q", ""), tail, mark))
        for kid in _children(asks, pruned, nid):
            walk(kid, depth + 1)

    for root in _children(asks, pruned, None):
        walk(root, 0)
    print("\n".join(lines) if lines else "(empty)")


def _frontier(events):
    asks = _asks(events)
    pruned = _pruned(events, asks)
    answers = _answers(events)
    dones = _dones(events)
    cur = _last(events, "focus")
    return [i for i in sorted(asks, key=_sort_key)
            if i not in pruned and i not in answers and i not in dones and i != cur]


def cmd_frontier(a):
    events = _load(a.log)
    asks = _asks(events)
    fr = _frontier(events)
    if not fr:
        print("(none)")
        return
    for i in fr:
        print("- [%s] %s" % (i, asks[i].get("q", "")))


def cmd_view(a):
    events = _load(a.log)
    asks = _asks(events)
    answers = _answers(events)
    cur = _last(events, "focus")
    fr = _frontier(events)
    if cur is None:
        print("(no current focus)")
        print("open branches: %d" % len(fr))
        return
    parts = cur.split(".")
    chain = [".".join(parts[:k + 1]) for k in range(len(parts))]
    print("current path:")
    for depth, nid in enumerate(chain):
        node = asks.get(nid, {})
        ans = answers.get(nid)
        tail = " → %s" % ans if ans else ""
        star = "  ← current" if nid == cur else ""
        print("%s- [%s] (%s) %s%s%s" % (
            "  " * depth, nid, node.get("stage", ""), node.get("q", ""), tail, star))
    print("open branches: %d" % len(fr))


def cmd_report(a):
    events = _load(a.log)
    asks = _asks(events)
    pruned = _pruned(events, asks)
    answers = _answers(events)
    dones = _dones(events)
    roots = _children(asks, pruned, None)
    title = a.title or (asks[roots[0]].get("q") if roots else "5 Whys")
    out = ["# 5 Whys report — %s" % title,
           "_Generated: %s · %d events_" % (_now(), len(events)), ""]
    out.append("## Starting point")
    for r in roots:
        out.append("- %s" % asks[r].get("q", ""))
    out += ["", "## Tree (What -> Example -> Why-not -> Benefits -> Costs)"]

    def walk(nid, depth):
        node = asks[nid]
        ans = answers.get(nid)
        tail = " → %s" % ans if ans else ""
        flag = "  **[root]**" if dones.get(nid, {}).get("kind") == "root" else (
            "  _[takeaway]_" if nid in dones else "")
        out.append("%s- **[%s]** (%s) %s%s%s" % (
            "  " * depth, nid, node.get("stage", ""), node.get("q", ""), tail, flag))
        for kid in _children(asks, pruned, nid):
            walk(kid, depth + 1)

    for r in roots:
        walk(r, 0)

    out += ["", "## Roots / key takeaways"]
    dn = [e for e in events if e.get("type") == "done" and e["id"] not in pruned]
    if dn:
        for e in dn:
            out.append("- **[%s]** (%s) %s" % (e["id"], e.get("kind", ""), e.get("note", "")))
    else:
        out.append("- (none yet)")

    disc = [e for e in events if e.get("type") == "discuss"]
    if disc:
        out += ["", "## Side discussions"]
        for e in disc:
            out.append("- @[%s] %s" % (e.get("anchor", ""), e.get("text", "")))
    print("\n".join(out))


def cmd_validate(a):
    events = _load(a.log)
    issues = []
    asks = {}
    for n, e in enumerate(events, 1):
        if "type" not in e or "ts" not in e:
            issues.append("line %d: missing type/ts" % n)
            continue
        t = e["type"]
        if t == "ask":
            if "id" not in e or "q" not in e:
                issues.append("line %d: ask missing id/q" % n)
                continue
            if e["id"] in asks:
                issues.append("line %d: duplicate ask id %r" % (n, e["id"]))
            p = e.get("parent")
            if p is not None and p not in asks:
                issues.append("line %d: ask parent %r not yet defined" % (n, p))
            asks[e["id"]] = e
        elif t in ("answer", "focus", "done", "prune"):
            if e.get("id") not in asks:
                issues.append("line %d: %s references unknown id %r" % (n, t, e.get("id")))
        elif t == "discuss":
            if e.get("anchor") not in asks:
                issues.append("line %d: discuss anchor %r unknown" % (n, e.get("anchor")))
        # unknown (plugin) types are allowed and ignored
    if issues:
        print("\n".join(issues))
        sys.exit(1)
    print("OK — %d events, %d nodes" % (len(events), len(asks)))


def main():
    ap = argparse.ArgumentParser(description="five-whys event-log mechanical core")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn):
        p = sub.add_parser(name)
        p.add_argument("log")
        p.set_defaults(fn=fn)
        return p

    p = add("ask", cmd_ask)
    p.add_argument("--parent", default=None)
    p.add_argument("--stage", default="")
    p.add_argument("--q", required=True)
    p.add_argument("--origin", default="proposed", choices=["proposed", "user"])

    p = add("answer", cmd_answer)
    p.add_argument("--id", required=True)
    p.add_argument("--a", required=True)
    p.add_argument("--source", default="ai")

    p = add("focus", cmd_focus)
    p.add_argument("--id", required=True)

    p = add("done", cmd_done)
    p.add_argument("--id", required=True)
    p.add_argument("--kind", default="root", choices=["root", "takeaway"])
    p.add_argument("--note", default="")

    p = add("discuss", cmd_discuss)
    p.add_argument("--anchor", required=True)
    p.add_argument("--text", required=True)

    p = add("prune", cmd_prune)
    p.add_argument("--id", required=True)
    p.add_argument("--reason", default="")

    p = add("event", cmd_event)
    p.add_argument("--json", required=True)

    add("view", cmd_view)
    add("tree", cmd_tree)
    add("frontier", cmd_frontier)

    p = add("report", cmd_report)
    p.add_argument("--title", default="")

    add("validate", cmd_validate)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
