/**
 * Telegram typing-indicator hook.
 *
 * On every inbound Telegram `message:received` event, POSTs sendChatAction(typing)
 * directly to the Telegram Bot API so the user sees the native "bot is typing…"
 * indicator within ~200ms — before model dispatch, skill invocation, or any
 * outbound message from the agent.
 *
 * IMPORTANT: uses `https.request` with `family: 4` instead of global `fetch`,
 * because the droplet's IPv6 routing to api.telegram.org times out and Node's
 * default fetch does not fall back (OpenClaw's own Telegram client logs the
 * same issue in gateway: "fetch fallback: enabling sticky IPv4-only
 * dispatcher"). Forcing IPv4 here mirrors that workaround.
 *
 * Fire-and-forget: we do not await the HTTP call and silently swallow errors.
 * Telegram auto-expires the indicator after ~5s; longer operations rely on
 * intermediate messages the skill emits directly.
 */

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import https from "node:https";

let _cachedToken;

async function getBotToken() {
    if (_cachedToken !== undefined) return _cachedToken;
    try {
        const configPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
        const body = await fs.readFile(configPath, "utf-8");
        const cfg = JSON.parse(body);
        _cachedToken = cfg?.channels?.telegram?.botToken ?? null;
    } catch {
        _cachedToken = null;
    }
    return _cachedToken;
}

function extractChatId(from) {
    // `from` format observed in OpenClaw sessions: "telegram:<chat_id>"
    const match = /^telegram:(.+)$/.exec(String(from ?? ""));
    return match ? match[1] : null;
}

function postSendChatAction(token, chatId, timeoutMs = 2000) {
    return new Promise((resolve) => {
        const body = JSON.stringify({ chat_id: chatId, action: "typing" });
        const req = https.request(
            {
                hostname: "api.telegram.org",
                port: 443,
                path: `/bot${token}/sendChatAction`,
                method: "POST",
                family: 4, // force IPv4; IPv6 times out on this droplet
                timeout: timeoutMs,
                headers: {
                    "Content-Type": "application/json",
                    "Content-Length": Buffer.byteLength(body),
                },
            },
            (res) => {
                res.resume();
                res.on("end", () => resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode, error: null }));
            },
        );
        req.on("error", (err) => resolve({ ok: false, status: 0, error: String(err).slice(0, 120) }));
        req.on("timeout", () => {
            req.destroy();
            resolve({ ok: false, status: 0, error: "timeout" });
        });
        req.write(body);
        req.end();
    });
}

const LOG_PATH = "/tmp/openclaw/telegram-typing-hook.log";

async function logLine(line) {
    try {
        const fs = await import("node:fs/promises");
        await fs.appendFile(LOG_PATH, `${new Date().toISOString()} ${line}\n`);
    } catch {
        // ignore
    }
}

const handler = async (event) => {
    const fireStart = Date.now();
    if (event?.type !== "message" || event?.action !== "received") return;

    const ctx = event.context ?? {};
    if (ctx.channelId !== "telegram") {
        await logLine(`skip (channelId=${ctx.channelId})`);
        return;
    }

    const chatId = extractChatId(ctx.from);
    if (!chatId) {
        await logLine(`skip (no chatId from '${ctx.from}')`);
        return;
    }

    const token = await getBotToken();
    if (!token) {
        await logLine(`skip (no bot token)`);
        return;
    }

    const fetchStart = Date.now();

    // Fire-and-forget via IPv4-forced https.request (not fetch — see module
    // docstring; fetch hits IPv6 timeouts on this droplet).
    postSendChatAction(token, chatId).then(async (res) => {
        if (res.ok) {
            await logLine(`chat=${chatId} fetch_ms=${Date.now() - fetchStart} status=${res.status} event_to_fetch_ms=${fetchStart - fireStart}`);
        } else {
            await logLine(`chat=${chatId} fetch_error=${res.error ?? `status=${res.status}`} fetch_ms=${Date.now() - fetchStart}`);
        }
    });

    await logLine(`invoked chat=${chatId} fire_ms=${Date.now() - fireStart} at ${new Date(fireStart).toISOString()}`);
};

export default handler;
