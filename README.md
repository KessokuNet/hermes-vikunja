# hermes-vikunja

A Hermes gateway platform adapter that makes **every Vikunja task a conversation**.

One task is one chat: an assignment or a human reply on the task becomes a message
in that task's Hermes session, and everything the agent says during the turn is
posted back as a comment on the same task. The board stays the record; the gateway
owns the conversation.

## Why a platform instead of a webhook receiver

The in-cluster receiver (`clusters/kessoku-chi1/vikunja/` in bocchi-cluster) did
this the hard way: it forwarded to `/v1/runs` with a made-up `session_id`, so
session continuity was emulated outside the gateway and the resulting conversation
was invisible in the dashboard. A platform adapter gets that from the gateway: the
per-task session store, `/new`, model overrides, cron delivery, and the board
becomes a real channel in `hermes status`. The receiver is gone; the reconciler
CronJob still exists to register the webhook on new projects and replay missed
assignments, now pointing at this listener.

## How it maps

| Vikunja | Hermes |
|---|---|
| task | chat `task-<id>` |
| `task.assignee.created` for a bot | inbound message that starts work |
| `@bot-kita` in a comment | inbound message: opens that task's conversation, or continues it |
| `task.comment.created` by someone else | inbound message only while the task is assigned to a bot |
| `project.updated` | no turn: the cached board description is dropped |
| the agent's messages during a turn | comments, one per message |
| the agent's own comments | not forwarded (loop guard) |
| a webhook delivery | HMAC-SHA256 of the raw body in `X-Vikunja-Signature` |

Every event in one task shares one session: the source is marked `shared_session`,
so a comment from the person who assigned the task continues the conversation the
assignment started, and the sender is labelled `[cappy] …` for the model.

A comment wakes the bot in two cases and stays quiet otherwise: a ping
(`@bot-kita`, any case) always arrives and opens the task's conversation if it had
none, and an ordinary comment arrives only while the task is assigned to a bot —
so two people discussing a task the bot does not own do not spend tokens.

Every turn also carries the task's current state — identifier, title, project,
priority, percent_done, due date, assignees, labels, relations and the markdown
description (cut at 2000 characters) — plus the board it lives on. A project
description is treated as the board's rules document: the project's own arrival is
sent up to 4000 characters under a `project description (rules for this board):`
label, and the nearest ancestor that has one is carried too, attributed as
`parent board: <name> (house rules):` and cut at 2000, because house rules usually
live a level up from the leaf. Anything cut says so and tells the agent to read the
board in full before acting.

The project read is cached for ten minutes — and every project is subscribed to
`project.updated`, which the adapter answers by dropping the cache, so an edited
rule reaches the next turn instead of waiting out the window. (Editing a parent
drops every cached board, since children quote ancestors.) All of it rides as the
event's `channel_context`: a fresh read of the task per turn, so a label or status
the agent set with the board tools is visible on the next message without another
`tasks_read`, and because it sits outside the message text a `/new` comment still
parses as a command.

Comments are flat: Vikunja's `TaskComment` has no parent field, so a reply cannot
quote or thread — it is simply the next comment. Inbound bodies written by the
board's MCP tools arrive as HTML and are normalised to markdown before the model
sees them; outbound messages are rendered from markdown to HTML (Python-Markdown,
`fenced_code`/`tables`/`sane_lists`) because the board renders a comment as HTML and
markdown posted verbatim shows up with its asterisks and backticks intact.

## Configuration

`~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    vikunja:
      enabled: true
      extra:
        base_url: https://tasks.cappuchino.xyz
        # The tailnet address Vikunja in chi1 can reach; loopback is unreachable
        # from the cluster.
        listen: 100.108.220.66:8790
        bots: bot-kita,bot-agent

display:
  platforms:
    vikunja:
      streaming: false
      show_reasoning: false
      tool_progress: "off"
```

The plugin registers `display_tier: high` with those three keys overridden, but a
profile whose global `display.streaming` / `display.show_reasoning` is set to `true`
still wins over the tier default — a Vikunja comment is a permanent record with no
edit API, so reasoning and streaming chunks arrive as extra comments. Set the
per-platform `display.platforms.vikunja` block. The tier is what keeps
`interim_assistant_messages` on and tool-progress bubbles off.

Secrets in the profile's `.env`:

```
VIKUNJA_URL=https://tasks.cappuchino.xyz
VIKUNJA_TOKEN=<bot-kita's API token>
VIKUNJA_HOOK_SECRET=<the webhook secret>
VIKUNJA_HOME_CHANNEL=task-<id>   # optional: silences the per-chat /sethome notice
```

`VIKUNJA_BOTS` (default `bot-kita,bot-agent`) lists the usernames whose own comments
are not forwarded back as new turns.

Each project needs a webhook at `http://<gateway>:8790/webhooks/vikunja` for
`task.assignee.created` and `task.comment.created`. Vikunja routes every event to
the webhooks of the task's project **and of all its ancestors**
(`projectWebhooksForEvent` walks `ProjectAncestor`), so a subproject is covered by
its parent's hook from the moment it exists. The reconciler CronJob still registers
a hook on the project itself — and shares it with the bot — within a few minutes, so
the subproject stays covered if it is later detached from its parent; the adapter's
event-identity dedupe collapses the double delivery that having both produces.

The window that does hurt is a new **top-level** project: it has no ancestor, so an
assignment made before the reconciler's next pass is delivered nowhere until
`--sweep` replays it into the same endpoint. Vikunja refuses non-routable webhook
targets unless `VIKUNJA_WEBHOOKS_ALLOWNONROUTABLEIPS` is true, which it is here.

## Installing

```bash
just install          # symlink this checkout into ~/.hermes/plugins/hermes-vikunja
just validate         # hermes plugins validate on the installed directory
hermes plugins enable hermes-vikunja
```

`just install` also points `~/.hermes/vikunja-a2a.md` at the manual in this repo
(`vikunja-a2a.md`), keeping the unversioned copy aside as a `.bak-*` file. The
manual is the board's operating rules, read by the agent on every assignment, so
it belongs in git next to the code that honours it.

A code change needs a gateway restart; `hermes plugins enable` only re-wires
handlers and leaves the live adapter (and its imported module) in place.

## Development loop

The gateway runs on cat and this checkout lives on kikuri:

```bash
just sync     # one-way rsync of the checkout onto the gateway host
just deploy   # sync, then ask the gateway for a restart that waits for idle
just watch    # poll the tree, sync every change, restart when adapter/__init__/plugin.yaml moved
```

`hermes-vikunja-sync.service` (a user unit on kikuri, `just watch` under systemd)
runs the same loop continuously, so an edit is on cat about two seconds later and
the gateway restarts between turns rather than under one. Stop it with
`systemctl --user stop hermes-vikunja-sync` when a deployment has to stay still.

## Status

Verified end to end on cat against Vikunja v2.8.0:

* a real `task.assignee.created` delivery arrives at the listener over the tailnet,
  is verified against the webhook secret, and starts a turn in `task-<id>`;
* the reply lands as a comment on that task, plus the interim messages the agent
  writes during the turn, and nothing else (no reasoning, no tool-progress);
* a later human comment on the same task continues the *same* session — the agent
  answered a follow-up from its own history with a single API call, and named the
  task's labels and priority from the injected context without a board read;
* `/new` in a comment resets that task's session and says so;
* subprojects work before the reconciler reaches them, through the parent's hook;
* unsigned deliveries are refused with 401, and a dedupe window keyed on the event
  identity (comment id, assignee id) collapses Vikunja's retries and the
  parent-plus-child delivery — the body is re-marshalled with a fresh `time` on a
  retry, so hashing it is not enough.

Still missing, in rough order of how much they hurt:

1. **Approval cards.** `handle_message`/`send` have no button support, so an
   approval or clarify question arrives as plain text and the answer comes back as
   a normal comment — a whole extra turn.
2. **Edits.** Vikunja comments cannot be edited through the API, so streaming mode
   would be a stream of separate comments. Hence `streaming: false`.
3. **Attachments.** Media from the agent is not uploaded to the task; replies are
   text only (Vikunja does have task attachments, the adapter just doesn't use them).
4. **Per-task home.** Cron results go to `VIKUNJA_HOME_CHANNEL`; without one set,
   every new task's first turn carries the "/sethome" notice as a comment.

## Layout

```
plugin.yaml   manifest — kind: platform, env the adapter needs
__init__.py   package entry point: from .adapter import register
adapter.py    VikunjaAdapter + register()
justfile      install / check / validate
```

<!-- watch test -->
