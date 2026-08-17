# five-whys — a guided dive-deep research mode

A KiroCrew skill that turns "understand this deeply" into a disciplined,
branching investigation (COE — Correction of Errors — 5 Whys style): **one
focused question at a time, short answers, natural branching**, with every step
recorded so the final report is a pure fold of the record. 5 Whys is the
*default engine*; the design is a small **research method** that other
capabilities can plug into.

## What it does

- Asks **one direct question per turn**, keeps each answer to **1-2 sentences**.
- Orders the dive along a reasoning chain: **What it is -> Example -> Why not the
  alternatives -> Benefits -> Costs**.
- Proposes 2-4 next-layer branch questions each turn; the **user steers** (pick
  one, pick several, or type their own — the user's question always wins).
- Supports **side discussions** anchored to a node, non-destructive by default.
- Records everything to an **event log**, then folds it into a report on
  close-out.

## Architecture (event-sourced)

The whole thing is built on **one append-only event log = the single source of
truth**. Everything else is a projection over it.

```
      Engine (5 Whys)        Capabilities (plugins: web search, recap, ...)
            \                        /
             \   append typed events (via scripts/five_whys.py)
              v                      v
      +---------------------------------------------+
      |   EVENT LOG  (append-only JSONL)  = state    |
      |   ask . answer . focus . done . discuss .    |
      |   prune . + plugin custom types              |
      +---------------------------------------------+
                         |  fold  (deterministic, never stored)
                         v
      Projections: tree . current question . frontier . roots . report
                         |  render        ^  user actions -> new events
                         v                |
      Frontends (also projections): chat bubble . web page . markdown report
```

Two layers, coupled only through the log:

- **Skill (judgment)** — the LLM decides *what* to ask, whether an answer is a
  real cause, when a thread is done. Lives in `SKILL.md`.
- **Script (mechanics)** — `scripts/five_whys.py` owns the deterministic parts:
  append + schema validation, node-id allocation, referential integrity, and
  folding the log into projections/report. The LLM never hand-writes JSONL.

Because every projection **ignores event types it doesn't recognize**, a new
capability (or a new frontend) plugs in without breaking anything — it just
reads the log and appends its own typed events.

## Files

| File | Role |
|------|------|
| `SKILL.md` | The behavior contract — how the agent runs the mode. |
| `scripts/five_whys.py` | Deterministic event-log CLI (stdlib only, Python 3.8+). |

## Event types

| type | meaning |
|------|---------|
| `ask` | a question node; `parent` field records the tree relationship |
| `answer` | the answer to a node |
| `focus` | cursor — latest `focus` is the current question |
| `done` | a thread bottomed out (root / takeaway) |
| `discuss` | a side-discussion entry, off-tree, anchored to a node |
| `prune` | remove a node/branch (a new event, never a rewrite) |

Plugins extend this set with their own types (e.g. `search`, `recap`).

## Using it

Trigger with **"5 whys" / "five whys"**, then the agent drives the loop. The log
lands at `<project>/five-whys/<slug>-<YYYY-MM-DD>.jsonl`. At any point:

```
python3 scripts/five_whys.py view   <log>    # current path + open-branch count
python3 scripts/five_whys.py tree   <log>    # full folded tree
python3 scripts/five_whys.py report <log>    # fold to a markdown report
python3 scripts/five_whys.py validate <log>  # schema + integrity gate (exit 0/1)
```

## Extending (plugins)

A capability declares: an **invocation**, **reads** the shared log, **emits** its
own event `type`(s) — marked *structural* (`ask`/`prune`, changes the tree) or
*annotation* (everything else, only enriches a node) — and follows the same
one-thing-then-stop discipline. See the "Capabilities" section in `SKILL.md`.

## Design principles

1. **Append-only** — never rewrite the log; corrections are new events.
2. **Projections fold, never drift** — the tree/report are always recomputed.
3. **Skill judges, script mechanizes** — determinism where it matters, judgment
   where it matters.
4. **One log, many plugs** — engine, capabilities, and frontends couple only
   through the event log.
