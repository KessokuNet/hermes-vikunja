"""Vikunja as a Hermes gateway platform.

One Vikunja task is one chat. The adapter turns the board into a conversation:

* inbound — a project webhook (``task.assignee.created``, ``task.comment.created``)
  arrives on a small HTTP listener, is verified against the HMAC secret, and is
  handed to the gateway as a message in chat ``task-<id>``, so a reply lands in
  the same Hermes session that took the assignment;
* outbound — the agent's answer is posted back as a comment on that task, which
  is the record the human actually reads.

Vikunja signs each delivery with ``X-Vikunja-Signature`` (hex HMAC-SHA256 of the
raw body) and retries a failed delivery with backoff, so the dedupe window has to
outlast the retry budget. The delivery is the credential: only the webhook secret
can produce a valid signature, and who may comment is decided by the project's
ACL on Vikunja's side.
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import logging
import re
import time
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, request_never_left
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, platform_http_client

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/vikunja"
HEALTH_PATH = "/healthz"
DEFAULT_LISTEN = "127.0.0.1:8790"
DEFAULT_BOTS = ("bot-kita", "bot-agent")
# PostgreSQL columns hold 1 MiB; staying well under it keeps a reply one comment.
MAX_MESSAGE_LENGTH = 60_000
# Vikunja retries a failed delivery within seconds; the window only has to cover
# that burst, so a genuine remove-then-re-add of an assignee still counts as new.
DEDUP_SECONDS = 120
MAX_BODY_BYTES = 1 << 20
TASK_CHAT = re.compile(r"^task-(\d+)$")
# Board state rides along with every turn; a long description is cut, not re-sent in full.
CONTEXT_DESCRIPTION_CHARS = 2000
# The thread a task already has, carried into the assignment turn: without it the
# agent starts blind on a task people have been discussing for days.
COMMENT_HISTORY_LIMIT = 15
COMMENT_HISTORY_CHARS = 400
COMMENT_HISTORY_TOTAL_CHARS = 4000
# A board's description is where its rules live, so the ceiling is generous: one
# infr-sized rules document (1 kB) plus room for the sections people append.
PROJECT_DESCRIPTION_CHARS = 4000
# House rules usually sit on the parent board; carry the nearest ancestor that has any.
PARENT_DESCRIPTION_CHARS = 2000
PARENT_LOOKUP_DEPTH = 3
# Safety net only: a ``project.updated`` delivery evicts the entry immediately.
PROJECT_CACHE_SECONDS = 600
# The bucket a picked-up task moves to, matched by title on the project's kanban view.
DOING_BUCKET_NAMES = ("doing", "in progress", "in-progress", "wip")
KANBAN_CACHE_SECONDS = 600
# Only tags a Vikunja editor actually emits; anything else in angle brackets is
# markdown (``<area>/<topic>``, ``<model>``) and must survive untouched.
_HTML_HINT = re.compile(
    r"</?(?:p|br|ul|ol|li|code|pre|strong|b|em|i|a|h[1-6]|blockquote|del|span|div)\b[^>]*>",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    """Board text → the markdown the model should read.

    Vikunja stores a description or comment exactly as written, so one written in the
    board's editor arrives as HTML while one posted by this adapter arrives as
    markdown. Converting only the tags an editor emits keeps HTML out of the prompt
    without eating markdown placeholders such as ``<area>/<topic>``.
    """
    if not text or not _HTML_HINT.search(text):
        return text
    out = re.sub(r"<br\s*/?>", "\n", text)
    out = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + _HTML_HINT.sub("", m.group(1)).strip("\n") + "\n```\n", out, flags=re.S)
    out = re.sub(r"</p>\s*", "\n\n", out)
    out = re.sub(r"<p[^>]*>\s*", "", out)
    out = re.sub(r"<li[^>]*>\s*", "- ", out)
    out = re.sub(r"</li>\s*", "\n", out)
    out = re.sub(r"</?(?:ul|ol)[^>]*>\s*", "", out)
    out = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", out, flags=re.S)
    out = re.sub(r"<(?:strong|b)>(.*?)</(?:strong|b)>", r"**\1**", out, flags=re.S)
    out = re.sub(r"<(?:em|i)>(.*?)</(?:em|i)>", r"*\1*", out, flags=re.S)
    out = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "#" * int(m.group(1)) + " " + m.group(2), out, flags=re.S)
    out = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", out, flags=re.S)
    out = html_lib.unescape(out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _html(text: str) -> str:
    """Markdown → the HTML Vikunja renders.

    A comment is stored exactly as written and the board renders it as HTML (its own
    editor posts HTML), so markdown sent verbatim shows up with its asterisks and
    backticks intact. Render it here; without Python-Markdown, post as written.
    """
    try:
        import markdown
    except ImportError:  # pragma: no cover - ships with the Hermes venv
        return text
    return markdown.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists"], output_format="html"
    )


def _username(user: Any) -> str:
    return str(user.get("username") or "") if isinstance(user, dict) else ""


class VikunjaAdapter(BasePlatformAdapter):
    """A chat per task, backed by Vikunja's REST API and webhooks."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("vikunja"))
        extra = getattr(config, "extra", None) or {}
        self.base_url = (extra_or_secret(extra, "base_url", "VIKUNJA_URL") or "").rstrip("/")
        self.token = extra_or_secret(extra, "token", "VIKUNJA_TOKEN") or ""
        self.hook_secret = extra_or_secret(extra, "hook_secret", "VIKUNJA_HOOK_SECRET") or ""
        listen = extra_or_secret(extra, "listen", "VIKUNJA_LISTEN") or DEFAULT_LISTEN
        host, _, port = listen.rpartition(":")
        self.listen_host = host or "127.0.0.1"
        try:
            self.listen_port = int(port)
        except ValueError:
            self.listen_host, self.listen_port = "127.0.0.1", 8790
        bots = extra_or_secret(extra, "bots", "VIKUNJA_BOTS") or ",".join(DEFAULT_BOTS)
        self.bot_usernames = {name.strip() for name in bots.split(",") if name.strip()}
        self._runner: Any = None
        self._http: Any = None
        self._projects: dict[int, tuple[float, list[str]]] = {}
        self._kanban: dict[int, tuple[float, int, int]] = {}
        self._dedup = MessageDeduplicator(max_size=1000, ttl_seconds=DEDUP_SECONDS)
    # -- lifecycle -------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.base_url or not self.token or not self.hook_secret:
            missing = [
                name for name, value in (
                    ("VIKUNJA_URL", self.base_url),
                    ("VIKUNJA_TOKEN", self.token),
                    ("VIKUNJA_HOOK_SECRET", self.hook_secret),
                ) if not value
            ]
            return self._fail("config_missing", f"vikunja: {', '.join(missing)} is not set", retryable=False)
        try:
            from aiohttp import web
        except ImportError:
            return self._fail("deps_missing", "vikunja: aiohttp is required to receive webhooks", retryable=False)

        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_post(WEBHOOK_PATH, self._on_webhook)
        app.router.add_get(HEALTH_PATH, self._on_health)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.listen_host, self.listen_port).start()
        except OSError as exc:
            await runner.cleanup()
            return self._fail(
                "listen_failed",
                f"vikunja: cannot listen on {self.listen_host}:{self.listen_port}: {exc}",
                retryable=True,
            )
        self._runner = runner
        self._http = platform_http_client(headers={"Authorization": f"Bearer {self.token}"})
        listener = f"http://{self.listen_host}:{self.listen_port}"
        self._mark_connected(listener_base=listener)
        log.info("[vikunja] %s listening on %s%s", self.name, listener, WEBHOOK_PATH)
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._mark_disconnected()

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        log.error("[vikunja] %s", message)
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    # -- inbound ---------------------------------------------------------

    async def _on_health(self, _request):
        from aiohttp import web

        return web.json_response(
            {"status": "ok" if self._running else "down", "listen": f"{self.listen_host}:{self.listen_port}"}
        )

    def _signed(self, body: bytes, given: str) -> bool:
        if not self.hook_secret or not given:
            return False
        expected = hmac.new(self.hook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(given, expected)

    @staticmethod
    def _delivery_key(document: dict) -> str:
        """Identity of one logical event, for Vikunja's at-least-once retries.

        The body hash is not enough: a retried delivery is re-marshalled with a fresh
        ``time``, so the bytes differ while the event is the same. The id in the event
        (comment id, assignee id) is stable across retries, and a genuine
        remove-then-re-add of an assignee falls outside the dedupe window.
        """
        event = str(document.get("event_name") or "")
        data = document.get("data") or {}
        task = (data.get("task") or {}).get("id")
        if event == "task.comment.created":
            return f"{event}:{task}:{(data.get('comment') or {}).get('id')}"
        if event.startswith("task.assignee"):
            return f"{event}:{task}:{(data.get('assignee') or {}).get('id')}"
        if event.startswith("project."):
            # Every edit is a distinct cache bust, so the timestamp is part of the key.
            return f"{event}:{(data.get('project') or {}).get('id')}:{document.get('time', '')}"
        return f"{event}:{task}"

    async def _on_webhook(self, request):
        from aiohttp import web

        body = await request.read()
        if not self._signed(body, request.headers.get("X-Vikunja-Signature", "")):
            log.warning("[vikunja] rejected an unsigned delivery (%d bytes)", len(body))
            return web.Response(status=401, text='{"error":"bad signature"}')
        try:
            document = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return web.Response(status=400, text='{"error":"bad json"}')
        key = self._delivery_key(document)
        if self._dedup.is_duplicate(key):
            log.info("[vikunja] duplicate delivery %s", key)
            return web.Response(status=202, text='{"status":"duplicate"}')
        try:
            outcome = await self._deliver(document)
        except Exception:  # a bad event must not make Vikunja retry forever
            log.exception("[vikunja] could not deliver %s", document.get("event_name"))
            outcome = "error"
        return web.Response(status=202, text=json.dumps({"status": outcome}))

    async def _deliver(self, document: dict) -> str:
        event = str(document.get("event_name") or "")
        data = document.get("data") or {}
        if event in ("project.updated", "project.deleted"):
            return self._evict_project(event, data)
        task = data.get("task") or {}
        task_id = task.get("id")
        if not task_id:
            return "ignored:no-task"
        if event == "task.assignee.created":
            return await self._deliver_assignment(document, data, task, int(task_id))
        if event == "task.comment.created":
            return await self._deliver_comment(document, data, task, int(task_id))
        return f"ignored:{event or 'unknown'}"

    def _evict_project(self, event: str, data: dict) -> str:
        """A rules document changed: drop cached copies so the next turn sees it.

        The whole cache goes, not just this project: a child's context quotes the
        nearest described ancestor, so editing a parent's rules invalidates the
        children too.
        """
        project_id = (data.get("project") or {}).get("id")
        dropped = len(self._projects)
        self._projects.clear()
        log.info(
            "[vikunja] %s for project %s, dropped %d cached board description(s)",
            event, project_id or "?", dropped,
        )
        return f"ignored:{event}"

    async def _deliver_assignment(self, document: dict, data: dict, task: dict, task_id: int) -> str:
        assignee = _username(data.get("assignee"))
        if assignee not in self.bot_usernames:
            return f"ignored:assigned-to-{assignee or 'nobody'}"
        # The person who assigned it is the sender; reporting the assignee would
        # make the gateway read its own assignment as a message from itself.
        doer = _username(data.get("doer"))
        body = await self._assignment_prompt(task_id, data)
        await self._move_to_doing(task, task_id)
        await self._submit(
            document,
            chat_id=f"task-{task_id}",
            chat_name=f"#{task_id} {task.get('title') or ''}".strip(),
            text=body,
            sender=doer or assignee,
            message_id=f"assignee:{task_id}:{assignee}:{document.get('time', '')}",
            allow_commands=False,
        )
        log.info("[vikunja] task %s assigned to %s by %s", task_id, assignee, doer or "(unknown)")
        return "assigned"

    async def _deliver_comment(self, document: dict, data: dict, task: dict, task_id: int) -> str:
        comment = data.get("comment") or {}
        author = _username(data.get("doer")) or _username(comment.get("author"))
        if not author:
            return "ignored:anonymous"
        if author in self.bot_usernames:
            return f"ignored:own-comment-{author}"
        text = _plain(comment.get("comment") or "").strip()
        if not text:
            return "ignored:empty"
        # A ping opens the task's conversation; an ordinary comment only continues one
        # the bot already owns, so people talking to each other stay quiet.
        mentioned = self._mentions(text)
        if not mentioned and not await self._owned_by_bot(task, task_id, data):
            log.info(
                "[vikunja] comment %s on task %s by %s ignored (no ping, task is not the bot's)",
                comment.get("id"),
                task_id,
                author,
            )
            return "ignored:not-addressed"
        await self._move_to_doing(task, task_id)
        await self._submit(
            document,
            chat_id=f"task-{task_id}",
            chat_name=f"#{task_id} {task.get('title') or ''}".strip(),
            text=text,
            sender=author,
            message_id=f"comment:{comment.get('id') or document.get('time', '')}",
            allow_commands=True,
            context=await self._task_context(task_id, data),
        )
        log.info(
            "[vikunja] comment %s on task %s by %s%s",
            comment.get("id"),
            task_id,
            author,
            f" (mentions {', '.join(sorted(mentioned))})" if mentioned else "",
        )
        return "comment"

    async def _move_to_doing(self, task: dict, task_id: int) -> None:
        """Card lifecycle: a task the bot has picked up belongs in Doing, not To-Do.

        Vikunja only moves a card through
        ``PUT /projects/{p}/views/{v}/buckets/{b}/tasks`` — the task update endpoint
        accepts ``bucket_id`` and silently ignores it — and the route needs the
        ``views_buckets_tasks_update`` permission on the token. Best effort: a failure
        is logged and never blocks the turn.
        """
        try:
            if task.get("done"):
                return
            project_id = int(task.get("project_id") or 0)
            if project_id <= 0:
                return
            cached = self._kanban.get(project_id)
            if not cached or cached[0] <= time.monotonic():
                view_id, bucket_id = await self._resolve_doing_bucket(project_id)
                if not view_id or not bucket_id:
                    return
                cached = (time.monotonic() + KANBAN_CACHE_SECONDS, view_id, bucket_id)
                self._kanban[project_id] = cached
            _, view_id, bucket_id = cached
            path = f"/api/v2/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks"
            moved = await self._api("PUT", path, {"task_id": task_id})
            if moved is None:  # older builds register this route as POST
                moved = await self._api("POST", path, {"task_id": task_id})
            if moved is not None:
                log.info("[vikunja] task %s in project %s moved to bucket %s", task_id, project_id, bucket_id)
        except Exception:  # a card move must never cost the turn
            log.warning("[vikunja] could not move task %s to Doing", task_id, exc_info=True)

    async def _resolve_doing_bucket(self, project_id: int) -> tuple[int, int]:
        """The project's kanban view and its Doing bucket, by title."""
        views = await self._api("GET", f"/api/v2/projects/{project_id}/views") or {}
        items = views.get("items") if isinstance(views, dict) else views
        view_id = next((int(v["id"]) for v in items or [] if v.get("view_kind") == "kanban"), 0)
        if not view_id:
            return 0, 0
        buckets = await self._api("GET", f"/api/v2/projects/{project_id}/views/{view_id}/buckets") or {}
        bucket_items = buckets.get("items") if isinstance(buckets, dict) else buckets
        bucket_id = next(
            (
                int(b["id"])
                for b in bucket_items or []
                if str(b.get("title") or "").strip().lower() in DOING_BUCKET_NAMES
            ),
            0,
        )
        return view_id, bucket_id

    def _mentions(self, text: str) -> set[str]:
        """Bot usernames the text pings, in any case: Vikunja mentions are plain text."""
        found = set()
        for name in self.bot_usernames:
            if re.search(rf"(?<![\w@])@{re.escape(name)}\b", text, re.IGNORECASE):
                found.add(name)
        return found

    async def _owned_by_bot(self, task: dict, task_id: int, data: dict) -> bool:
        """Is this task already the bot's conversation (assigned to one of its identities)?"""
        assignees = task.get("assignees")
        if assignees is None:
            assignees = (await self._task(task_id, data)).get("assignees") or []
        return any(_username(user) in self.bot_usernames for user in assignees or [])

    async def _submit(
        self,
        document: dict,
        *,
        chat_id: str,
        chat_name: str,
        text: str,
        sender: str,
        message_id: str,
        allow_commands: bool,
        context: str = "",
    ) -> None:
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name[:120],
            chat_type="group",
            user_id=sender,
            user_name=sender,
            message_id=message_id,
        )
        # A task is one conversation: an assignment and every later comment share it
        # whoever wrote them, instead of one session per participant.
        source.shared_session = True
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=sender,
            user_name=sender,
            source=source,
            raw_message=document,
            message_id=message_id,
            allow_gateway_control=allow_commands,
            # The board state rides outside the message text, so the trigger still
            # parses as a command and the transcript keeps only what was written.
            channel_context=context or None,
        )
        # handle_message() returns after spawning the turn; the session key is the
        # chat id, so an assignment and every later comment share one conversation.
        await self.handle_message(event)

    def _header(self, task: dict, task_id: int) -> list[str]:
        """The board state a turn needs, whoever triggered it."""
        labels = ", ".join(label.get("title", "") for label in (task.get("labels") or []))
        assignees = ", ".join(_username(user) for user in (task.get("assignees") or []))
        due = str(task.get("due_date") or "").replace("0001-01-01T00:00:00Z", "")
        lines = [
            f'Task: {task.get("identifier") or ""} #{task_id} "{task.get("title", "")}" '
            f'(project {task.get("project_id") or "?"})',
            f'priority {task.get("priority", 0)} (0 unset, 1 low, 3 normal, 5 do-now), '
            f'percent_done {task.get("percent_done", 0)}, due {due or "(none)"}',
            f"assignees: {assignees or '(none)'}",
            f"labels: {labels or '(none)'}",
        ]
        relations = [
            f"  {kind}: #{other.get('id')} {other.get('title', '')}"
            for kind, related in sorted((task.get("related_tasks") or {}).items())
            for other in related
            if other.get("id")
        ]
        if relations:
            lines.append("relations:")
            lines.extend(relations)
        return lines

    async def _task(self, task_id: int, data: dict) -> dict:
        return await self._api("GET", f"/api/v2/tasks/{task_id}?format=markdown") or data.get("task") or {}

    def _description(self, task: dict) -> str:
        description = (task.get("description") or "").strip() or "(empty)"
        if len(description) > CONTEXT_DESCRIPTION_CHARS:
            description = description[:CONTEXT_DESCRIPTION_CHARS].rstrip() + " …(truncated)"
        return description

    async def _task_context(self, task_id: int, data: dict) -> str:
        """Board state for every comment turn, so the agent never re-reads the task
        just to know its labels, priority or relations, or what board it is on."""
        task = await self._task(task_id, data)
        lines = ["Vikunja task (current state):"]
        lines.extend(self._header(task, task_id))
        lines.extend(await self._project_lines(task.get("project_id")))
        lines.extend(["", "description (markdown):", self._description(task)])
        return "\n".join(lines)

    async def _project_lines(self, project_id: Any) -> list[str]:
        """The board the task lives on: its title, where it sits, and its rules.

        A project description is a rules document, so it is sent close to whole, and
        the nearest ancestor with a description rides along attributed — house rules
        usually live on the parent board, not the leaf.
        """
        try:
            project_id = int(project_id or 0)
        except (TypeError, ValueError):
            return []
        if project_id <= 0:
            return []
        cached = self._projects.get(project_id)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])
        project = await self._api("GET", f"/api/v2/projects/{project_id}") or {}
        if not project:
            return []
        parent_id = int(project.get("parent_project_id") or 0)
        ancestor_id, ancestor = await self._nearest_described_ancestor(parent_id)
        head = f'project: {project.get("title") or f"#{project_id}"}'
        if project.get("identifier"):
            head += f' ({project["identifier"]})'
        if ancestor_id:
            head += f', subproject of {ancestor.get("title") or f"#{ancestor_id}"}'
        lines = [head]
        cut = self._description_block(
            lines, "project description (rules for this board):", project.get("description"),
            PROJECT_DESCRIPTION_CHARS,
        )
        if ancestor:
            cut = self._description_block(
                lines,
                f'parent board: {ancestor.get("title") or f"#{ancestor_id}"} (house rules):',
                ancestor.get("description"),
                PARENT_DESCRIPTION_CHARS,
            ) or cut
        if cut:
            lines.append("  …(truncated — read the board description in full before acting)")
        self._projects[project_id] = (time.monotonic() + PROJECT_CACHE_SECONDS, lines)
        return list(lines)

    @staticmethod
    def _description_block(lines: list[str], label: str, raw: Any, limit: int) -> bool:
        """Append ``label`` plus the description; returns True when it had to be cut."""
        description = _plain(str(raw or "")).strip()
        if not description:
            return False
        cut = len(description) > limit
        if cut:
            description = description[:limit].rstrip()
        lines.extend([label, description])
        return cut

    async def _nearest_described_ancestor(self, parent_id: int) -> tuple[int, dict]:
        """Nearest ancestor project that has a description, within a few levels."""
        for _ in range(PARENT_LOOKUP_DEPTH):
            if not parent_id:
                break
            project = await self._api("GET", f"/api/v2/projects/{parent_id}") or {}
            if not project:
                break
            if (project.get("description") or "").strip():
                return parent_id, project
            parent_id = int(project.get("parent_project_id") or 0)
        return 0, {}

    async def _comment_history(self, task_id: int) -> list[str]:
        """The task's existing comments, oldest first, bounded in count and length."""
        payload = await self._api("GET", f"/api/v2/tasks/{task_id}/comments?per_page=50") or {}
        items = payload.get("items") if isinstance(payload, dict) else payload
        comments = [c for c in (items or []) if _plain(c.get("comment") or "").strip()]
        if not comments:
            return []
        lines: list[str] = []
        budget = COMMENT_HISTORY_TOTAL_CHARS
        for comment in reversed(comments):  # newest first, so the freshest survives the cap
            body = _plain(comment.get("comment") or "").strip()
            if len(body) > COMMENT_HISTORY_CHARS:
                body = body[:COMMENT_HISTORY_CHARS].rstrip() + " …(cut)"
            author = _username(comment.get("author")) or "unknown"
            when = str(comment.get("created") or "")[:16].replace("T", " ")
            entry = f"  @{author} {when}: " + body.replace("\n", "\n    ")
            if len(entry) > budget or len(lines) >= COMMENT_HISTORY_LIMIT:
                break
            budget -= len(entry)
            lines.append(entry)
        lines.reverse()
        if len(lines) < len(comments):
            lines.insert(0, f"  ({len(comments) - len(lines)} earlier comment(s) not shown)")
        return lines

    async def _assignment_prompt(self, task_id: int, data: dict) -> str:
        """Task metadata, the thread so far, and the ask, for the turn that starts work."""
        task = await self._task(task_id, data)
        lines = [f'Assigned to you: {task.get("identifier") or ""} #{task_id} "{task.get("title", "")}"']
        lines.extend(self._header(task, task_id)[1:])
        lines.extend(await self._project_lines(task.get("project_id")))
        lines.extend(["", "description (markdown):", self._description(task)])
        history = await self._comment_history(task_id)
        if history:
            lines.extend(["", "comments on this task so far (oldest first):"])
            lines.extend(history)
        lines.extend([
            "",
            "Everything you write during this turn is posted to the task as a comment and the final "
            "answer is the last one, so say what you are doing there in short progress notes and "
            "skip the board's comment tool. Keep `percent_done` moving in the same rhythm with "
            "`tasks_update` (`project_id`, `task`, `percent_done`): a first bump now that you have "
            "picked this up, another after each material step, and `done` with 100 only when the "
            "result is verified. Read ~/.hermes/vikunja-a2a.md for the board conventions (project "
            "descriptions, relations, when to file and close).",
        ])
        return "\n".join(lines)

    # -- outbound --------------------------------------------------------

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict | None = None
    ) -> SendResult:
        # Vikunja comments are flat — no parent id, no threading — so ``reply_to`` has
        # nothing to attach to and the answer is simply the next comment.
        del reply_to, metadata
        match = TASK_CHAT.match(str(chat_id))
        if not match:
            return SendResult(success=False, error=f"not a task chat: {chat_id}")
        text = _text(content).strip() or "(no reply)"
        created = await self._api("POST", f"/api/v2/tasks/{match.group(1)}/comments", {"comment": _html(text)})
        if not isinstance(created, dict):
            return SendResult(success=False, error=f"vikunja rejected the comment on task {match.group(1)}")
        return SendResult(success=True, message_id=str(created.get("id") or ""))

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        match = TASK_CHAT.match(str(chat_id))
        if not match:
            return {"name": str(chat_id), "type": "group"}
        task = await self._api("GET", f"/api/v2/tasks/{match.group(1)}") or {}
        return {"name": task.get("title") or chat_id, "type": "group"}

    async def _api(self, method: str, path: str, payload: dict | None = None) -> Any:
        """One Vikunja API call; ``None`` on any failure so a turn degrades instead of crashing."""
        import httpx

        if self._http is None:
            self._http = platform_http_client(headers={"Authorization": f"Bearer {self.token}"})
        try:
            response = await self._http.request(method, f"{self.base_url}{path}", json=payload)
        except httpx.HTTPError as exc:
            log.warning("[vikunja] %s %s failed: %s (never sent: %s)", method, path, exc, request_never_left(exc))
            return None
        if response.status_code >= 400:
            log.warning("[vikunja] %s %s -> %s %s", method, path, response.status_code, response.text[:200])
            return None
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}


def check_requirements() -> bool:
    """Dependencies only: a passive probe that must not touch config or the network."""
    try:
        import aiohttp  # noqa: F401
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", None) or {}
    return bool(extra_or_secret(extra, "base_url", "VIKUNJA_URL"))


def register(ctx) -> None:
    ctx.register_platform(
        name="vikunja",
        label="Vikunja board",
        adapter_factory=VikunjaAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["VIKUNJA_URL", "VIKUNJA_TOKEN"],
        install_hint="No extra packages needed (aiohttp and httpx ship with Hermes)",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📋",
        # A comment is a permanent record: forward the assistant's messages, one per send,
        # with no reasoning dump, no tool-progress bubbles and no streaming edits.
        display_tier="high",
        display_overrides={
            "show_reasoning": False,
            "streaming": False,
            "tool_progress": "off",
            "long_running_notifications": False,
            "busy_ack_detail": False,
        },
        # Every delivery is signed with the webhook secret and Vikunja's project
        # ACL decides who may comment; there is no per-sender allowlist to consult.
        trusted_inbound=True,
        allow_update_command=False,
        platform_hint=(
            "You are replying on a Vikunja task. Each task is its own conversation; your messages "
            "are posted to that task as comments, rendered from markdown to HTML. Write standard "
            "markdown — a blank line before a list, fenced code blocks with the language — and "
            "treat the task description and comments as the record. Keep the task's fields honest "
            "as the work moves: set `percent_done` with `tasks_update` when you pick it up and "
            "after each material step, and set `done` (with `percent_done` 100) only once the "
            "result is verified — comments are the narrative, the fields are the status, and a "
            "task whose comments say finished but whose `percent_done` is 0 is still open."
        ),
    )
