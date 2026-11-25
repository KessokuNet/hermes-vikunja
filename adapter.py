"""Vikunja as a Hermes gateway platform.

One Vikunja task is one chat. The adapter turns the board into a conversation:

* inbound — a project webhook (`task.assignee.created`, `task.comment.created`)
  arrives on a small HTTP listener, is verified against the HMAC secret, and is
  handed to the gateway as a message in chat `task-<id>`, so the reply lands in
  the same Hermes session that took the assignment;
* outbound — the agent's answer is posted back as a comment on that task, which
  is the record the human actually reads.

This replaces a standalone webhook receiver that had to fake session continuity
with `session_id` on `/v1/runs`. Here the gateway's own per-chat session store
does it, and the conversation shows up in the dashboard like any other channel.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import re
from typing import Any

from gateway.platforms._shared import extra_or_secret, get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.config import Platform, PlatformConfig

log = logging.getLogger(__name__)

THREAD_ID = re.compile(r"mcp:(?:launch:)?[0-9a-fA-F-]{16,}")
DEFAULT_BOTS = ("bot-kita", "bot-agent")


def _supported(cls: Any, **candidates: Any) -> dict[str, Any]:
    """Keep only the kwargs this Hermes version's class actually declares.

    A platform plugin ships alongside the core it runs on, but the event types
    have gained fields across releases; filtering here means an older core or a
    newer one both construct.
    """
    fields = getattr(cls, "__dataclass_fields__", None) or {}
    names = set(fields) or set(inspect.signature(cls).parameters)
    return {k: v for k, v in candidates.items() if k in names}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


class VikunjaAdapter(BasePlatformAdapter):
    """A chat per task, backed by Vikunja's REST API and webhooks."""

    def __init__(self, config: PlatformConfig, **kwargs: Any) -> None:
        super().__init__(config, Platform("vikunja"), **kwargs)
        extra = getattr(config, "extra", None) or {}
        self.base_url = (extra_or_secret(extra, "base_url", "VIKUNJA_URL") or "").rstrip("/")
        self.token = extra_or_secret(extra, "token", "VIKUNJA_TOKEN") or ""
        self.hook_secret = extra_or_secret(extra, "hook_secret", "VIKUNJA_HOOK_SECRET") or ""
        listen = extra_or_secret(extra, "listen", "VIKUNJA_LISTEN") or "127.0.0.1:8790"
        host, _, port = listen.partition(":")
        self.listen_host = host or "127.0.0.1"
        self.listen_port = int(port or 8790)
        bots = extra_or_secret(extra, "bots", "VIKUNJA_BOTS") or ",".join(DEFAULT_BOTS)
        self.bot_usernames = {name.strip() for name in bots.split(",") if name.strip()}
        self._server = None

    # -- lifecycle -------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            from aiohttp import web
        except ImportError:  # pragma: no cover - aiohttp ships with the core gateway
            log.error("[vikunja] aiohttp is required to listen for webhooks")
            return False

        app = web.Application()
        app.router.add_post("/webhooks/vikunja", self._on_webhook)
        app.router.add_get("/healthz", lambda _request: web.Response(text='{"status":"ok"}'))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self.listen_host, self.listen_port).start()
        self._server = runner
        self._mark_connected()
        log.info("[vikunja] listening on %s:%s", self.listen_host, self.listen_port)
        return True

    async def disconnect(self) -> None:
        if self._server is not None:
            await self._server.cleanup()
            self._server = None
        self._mark_disconnected()

    # -- inbound ---------------------------------------------------------

    async def _on_webhook(self, request):
        from aiohttp import web

        body = await request.read()
        given = request.headers.get("X-Vikunja-Signature", "")
        expected = hmac.new(self.hook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not self.hook_secret or not hmac.compare_digest(given, expected):
            return web.Response(status=401, text='{"error":"bad signature"}')
        try:
            document = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return web.Response(status=400, text='{"error":"bad json"}')
        try:
            await self._deliver(document)
        except Exception:  # a bad event must not make Vikunja retry forever
            log.exception("[vikunja] could not deliver %s", document.get("event_name"))
        return web.Response(status=202, text='{"status":"accepted"}')

    async def _deliver(self, document: dict) -> None:
        event = document.get("event_name", "")
        data = document.get("data") or {}
        task = data.get("task") or {}
        task_id = task.get("id")
        if not task_id:
            return

        if event == "task.assignee.created":
            assignee = (data.get("assignee") or {}).get("username")
            if assignee not in self.bot_usernames:
                return
            body = await self._assignment_prompt(task_id, data)
            sender, sender_name = assignee, assignee
        elif event == "task.comment.created":
            comment = data.get("comment") or {}
            author = (data.get("doer") or comment.get("author") or {}).get("username")
            if not author or author in self.bot_usernames:
                return  # an agent's own comment is not a new turn
            body = (comment.get("comment") or "").strip()
            if not body:
                return
            sender, sender_name = author, author
        else:
            return

        event_obj = self._event(
            chat_id=f"task-{task_id}",
            text=body,
            sender_id=sender,
            sender_name=sender_name,
            message_id=f"{event}:{task_id}:{data.get('time', '')}",
        )
        await self.handle_message(event_obj)

    async def _assignment_prompt(self, task_id: int, data: dict) -> str:
        """The same shape the old receiver built: task metadata plus the ask."""
        task = await self._get(f"/api/v2/tasks/{task_id}?format=markdown") or data.get("task") or {}
        relations = []
        for kind, related in sorted((task.get("related_tasks") or {}).items()):
            for other in related:
                relations.append(f"  {kind}: #{other.get('id')} {other.get('title', '')}")
        labels = ", ".join(l.get("title", "") for l in (task.get("labels") or []))
        lines = [
            f'Assigned to you: {task.get("identifier") or ""} #{task_id} "{task.get("title", "")}"',
            f'priority {task.get("priority", 0)}, percent_done {task.get("percent_done", 0)}',
            f'labels: {labels or "(none)"}',
        ]
        if relations:
            lines.append("relations:")
            lines.extend(relations)
        lines.extend(["", "description (markdown):", (task.get("description") or "(empty)").strip()])
        lines.append(
            "\nRead ~/.hermes/vikunja-a2a.md for the board conventions. Comment status on the "
            "task as you go; your reply here is posted to the same task."
        )
        return "\n".join(lines)

    def _event(self, **candidates: Any) -> MessageEvent:
        source_cls = type(self).__mro__[0]  # placeholder, replaced below
        del source_cls
        try:
            from gateway.platforms.event import SessionSource  # type: ignore[attr-defined]
        except ImportError:  # older cores carry the fields on the event itself
            SessionSource = None
        fields = _supported(MessageEvent, **candidates)
        if SessionSource is not None:
            source = SessionSource(
                **_supported(
                    SessionSource,
                    platform="vikunja",
                    chat_id=candidates["chat_id"],
                    chat_type="group",
                    user_id=candidates["sender_id"],
                    user_name=candidates["sender_name"],
                )
            )
            fields["source"] = source
        fields.setdefault("message_type", MessageType.TEXT if hasattr(MessageType, "TEXT") else "text")
        return MessageEvent(**fields)

    async def _get(self, path: str) -> dict | None:
        return await self._request("GET", path)

    async def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        from gateway.platforms.helpers import platform_http_client

        async with platform_http_client(
            headers={"Authorization": f"Bearer {self.token}"}
        ) as client:
            response = await client.request(
                method, f"{self.base_url}{path}", json=payload
            )
            if response.status_code >= 400:
                log.warning("[vikunja] %s %s -> %s", method, path, response.status_code)
                return None
            return response.json() if response.content else None

    # -- outbound --------------------------------------------------------

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        del reply_to, metadata
        task_id = str(chat_id).removeprefix("task-")
        created = await self._request(
            "POST",
            f"/api/v2/tasks/{task_id}/comments",
            {"comment": _text(content)},
        )
        if not created:
            return SendResult(success=False, error="comment rejected")
        return SendResult(success=True, message_id=str(created.get("id") or ""))

    async def get_chat_info(self, chat_id) -> dict:
        task_id = str(chat_id).removeprefix("task-")
        task = await self._get(f"/api/v2/tasks/{task_id}") or {}
        return {"name": task.get("title") or f"task-{task_id}", "type": "group"}


def check_requirements() -> bool:
    return bool(get_scoped_secret("VIKUNJA_TOKEN")) and bool(get_scoped_secret("VIKUNJA_URL"))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", None) or {}
    return bool(extra_or_secret(extra, "token", "VIKUNJA_TOKEN"))


def register(ctx) -> None:
    ctx.register_platform(
        name="vikunja",
        label="Vikunja board",
        adapter_factory=VikunjaAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["VIKUNJA_TOKEN", "VIKUNJA_URL"],
        max_message_length=0,
        emoji="📋",
        platform_hint=(
            "You are replying on a Vikunja task. Each task is its own conversation; your reply "
            "is posted as a comment on that task. Keep answers in markdown, and treat the task "
            "description and comments as the record."
        ),
    )
