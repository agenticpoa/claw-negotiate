---
name: telegram-typing
description: "Show Telegram 'typing…' indicator immediately when a message arrives, before model dispatch"
metadata:
  {
    "openclaw": {
      "emoji": "⌛",
      "events": ["message:received"],
      "requires": { "config": ["channels.telegram.botToken"] }
    }
  }
---

# Telegram Typing Indicator

Fires on every inbound Telegram `message:received` event and posts to
Telegram's `sendChatAction(typing)` endpoint so the user sees the native
"bot is typing…" indicator within ~200ms of sending their message, instead
of waiting ~5-7s for the first bot message to arrive.

The indicator is purely a UX signal — it does not affect the message
pipeline. OpenClaw's normal routing (model dispatch, skill invocation,
etc.) proceeds in parallel; this hook is fire-and-forget, failures are
silently swallowed so they never block message handling.

The Telegram `typing` chat action auto-expires after ~5 seconds. For longer
operations, OpenClaw's own intermediate messages (e.g. "⏳ Parsing your
request…" pushed by the negotiate_safe skill) fill the gap.
