from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import INCOGNITO_MEMORY_MODES
from kiro_crew.sel import sel
from kiro_crew.session_summary import count_user_turns, extract_turns, render_input

logger = logging.getLogger(__name__)

_MAX_PURPOSE_CHARS = 500
_ASSISTANT_EXCERPT_CHARS = 600
_MAX_TRANSCRIPT_CHARS = 60_000
_TRUNCATION_MARKER = "\n\n[... earlier transcript omitted to fit the authoring budget ...]\n\n"

_SKILL_AUTHOR_INSTRUCTIONS = """\
Create a skill from this session.

You are a background subagent whose only job is to author one reusable Kiro Crew
skill from the chat transcript provided at the end of this message, then hand off.
Follow the `crystallize` skill in candidate mode:

- Stage the skill as a pending candidate under your Kiro Crew skills directory at
  `auto/.pending/<slug>/`, honoring $KIROCREW_HOME. Write both `SKILL.md` and the
  sibling `.meta.json`. This is a MANUAL, user-requested capture, so mark it as such:
  in `.meta.json` set `"namespace": "manual"`, `"name": "manual/<slug>"`, and
  `"source": "make-it-skill"`, and make the `SKILL.md` frontmatter `name:` read
  `manual/<slug>`. On approval it is promoted to a live skill under `manual/<slug>/`,
  keeping user-captured skills separate from auto-generated ones. Never write a live
  or active skill yourself, never write into the packaged `builtin_skills/` directory,
  and never write into a repository checkout.
- Reconstruct the reusable procedure from the whole transcript, folding in the
  working path from any `[Subagent completion event]` rows. Check the existing
  `auto/` and `manual/` skills first and freshen a near-duplicate rather than staging
  a second copy.
- Prefer prose steps. Add a Python helper script only when determinism earns it, keep
  it under 4 KB, and never let it read credentials, delete files, or call unknown hosts.
- Never include credentials, tokens, absolute paths, or personal data in the skill
  body, the metadata, or any script.

You do not have live access to the parent conversation; the transcript below is the
complete source. The candidate you stage appears in the user's Skills -> Pending review
for approval, so nothing loads until the user approves it.
"""

_PURPOSE_PREFIX = "\n\nThe user described the skill they want in one line:\n"
_PURPOSE_INFER = (
    "\n\nThe user did not describe the skill; infer its purpose and scope from the " "transcript.\n"
)
_TRANSCRIPT_HEADER = "\nTRANSCRIPT:\n\n"


async def api_create_skill_from_session(request: web.Request) -> web.Response:
    if request.get("app"):
        return web.json_response(
            {"error": "app token not permitted", "code": "app_forbidden"}, status=403
        )
    if request.get("internal_auth"):
        return web.json_response(
            {"error": "this endpoint is owner-only", "code": "human_only"}, status=403
        )
    if not is_owner_dashboard_request(request):
        return web.json_response({"error": "forbidden", "code": "forbidden"}, status=403)

    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"}, status=503
        )
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "conversation history unavailable", "code": "history_unavailable"},
            status=503,
        )

    body, err = await read_bounded_json(request)
    if err is not None or body is None:
        return err or web.json_response(
            {"error": "invalid JSON body", "code": "invalid_json"}, status=400
        )

    session_key = body.get("session_key")
    if not isinstance(session_key, str) or not session_key.strip():
        return web.json_response(
            {"error": "session_key is required", "code": "session_key_required"}, status=400
        )
    session_key = session_key.strip()

    purpose = body.get("purpose", "")
    if not isinstance(purpose, str):
        return web.json_response(
            {"error": "purpose must be a string", "code": "invalid_purpose"}, status=400
        )
    purpose = purpose.strip()
    if len(purpose) > _MAX_PURPOSE_CHARS:
        return web.json_response(
            {
                "error": f"purpose exceeds {_MAX_PURPOSE_CHARS} characters",
                "code": "purpose_too_long",
            },
            status=400,
        )

    slot_name = session_key.split(":", 1)[-1] if ":" in session_key else session_key
    slot = state._slots.get(slot_name)
    if slot is None:
        return web.json_response(
            {"error": f"unknown session {session_key!r}", "code": "unknown_session"}, status=404
        )

    if getattr(slot, "memory_mode", "") in INCOGNITO_MEMORY_MODES:
        return web.json_response(
            {"error": "cannot author a skill from a private session", "code": "incognito_session"},
            status=400,
        )

    history_key = slot_history_key(slot)
    records = await asyncio.to_thread(log.read_messages_chained, history_key)
    turns = extract_turns(list(records), assistant_excerpt_chars=_ASSISTANT_EXCERPT_CHARS)
    if count_user_turns(turns) < 1:
        return web.json_response(
            {"error": "session has nothing to author from", "code": "empty_session"}, status=400
        )

    shaped = render_input(turns)
    if len(shaped) > _MAX_TRANSCRIPT_CHARS:
        shaped = _TRUNCATION_MARKER + shaped[-_MAX_TRANSCRIPT_CHARS:]

    purpose_section = _PURPOSE_PREFIX + purpose + "\n" if purpose else _PURPOSE_INFER
    task = _SKILL_AUTHOR_INSTRUCTIONS + purpose_section + _TRANSCRIPT_HEADER + shaped

    parent_key = effective_session_key(slot)
    info = state.subagents.spawn(
        task,
        parent_session_key=parent_key,
        approval_mode="auto",
        silent=True,
        model=None,
        include_memory=True,
        include_lessons=True,
        include_project=True,
    )
    if not info:
        return web.json_response(
            {
                "error": f"capacity reached ({state.subagents.max_concurrent})",
                "code": "at_capacity",
            },
            status=429,
        )
    if info.done and info.error:
        return web.json_response({"error": info.error, "code": "spawn_rejected"}, status=400)

    try:
        sel().log_tool_invocation(
            session_key=parent_key,
            source="dashboard",
            tool_name="create_skill_from_session",
            outcome="invoked",
            request_id=info.id,
        )
    except Exception:
        logger.warning("SEL audit failed for create_skill_from_session", exc_info=True)

    logger.info("create_skill_from_session spawned subagent %s for %s", info.id, history_key)
    return web.json_response({"id": info.id, "status": "spawned"}, status=202)
