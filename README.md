# hermes-vikunja

A Hermes gateway platform adapter that makes **every Vikunja task a conversation**.

One task is one chat: an assignment or a human reply on the task becomes a message
in that task's Hermes session, and the agent's answer is posted back as a comment
on the same task. The board stays the record; the gateway owns the conversation.

## Why a platform instead of a webhook receiver

The in-cluster receiver (`clusters/kessoku-chi1/vikunja/` in bocchi-cluster) does
the same job the hard way: it forwards to `/v1/runs` with a made-up `session_id`,
so session continuity is emulated outside the gateway and the resulting
conversation is invisible in the dashboard. A platform adapter gets all of that
from the gateway: per-chat session store, streaming, approvals, `/new`, model
overrides, cron delivery — and the board becomes a real channel in
`hermes status`.

## How it maps

| Vikunja | Hermes |
|---|---|
| project | a directory whose chats are the project's tasks |
| task | chat `task-<id>` (thread = task) |
| `task.assignee.created` for a bot | inbound message that starts work |
| `task.comment.created` by a human | inbound message, continuing the same session |
| agent reply | `POST /api/v2/tasks/{id}/comments` |
| agent's own comment | not forwarded (loop guard) |

## Configuration

`~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    vikunja:
      enabled: true
      extra:
        base_url: https://tasks.cappuchino.xyz
        listen: 127.0.0.1:8790
        bots: bot-kita,bot-agent
```

Secrets go in the profile's `.env` (`VIKUNJA_TOKEN`, `VIKUNJA_HOOK_SECRET`), and
each project's webhook must target `http://<gateway host>:8790/webhooks/vikunja`
with `task.assignee.created` and `task.comment.created` selected.

## Status

First cut, not yet run against a live gateway. Written against the documented
plugin API (`ctx.register_platform`, `BasePlatformAdapter`, `MessageEvent`); the
event constructor is built reflectively because its fields differ across Hermes
releases. Next steps, in order:

1. `just install` on cat, `hermes plugins validate hermes-vikunja`, `hermes
   plugins enable hermes-vikunja` — fix whatever the validator reports.
2. Point one project's webhook at the listener and assign a task; confirm the
   comment arrives as a message and the reply lands as a task comment.
3. Retire the in-cluster receiver once this is the path in use.
4. Optional: post agent messages as markdown (already) and support `/new` to open
   a fresh session on a task without losing the comment trail.

## Layout

```
plugin.yaml   manifest — kind: platform, env the adapter needs
adapter.py    VikunjaAdapter + register()
justfile      install / check / validate
```
