#!/usr/bin/env node

/**
 * PushPlus (https://www.pushplus.plus/) notifier, shared by the manually
 * dispatched workflows: finance-digest-pushplus.yml pushes the digest, and
 * markdown-lint-manual.yml can push a lint report.
 *
 * It exists as a script rather than an inline `curl` step for one reason:
 * PushPlus reports failures with an HTTP 200 response whose JSON body carries
 * `code != 200`. A `curl --fail` step therefore exits 0 on a rejected push —
 * an invalid token, an unfollowed official account, an exhausted free quota or
 * an over-long body all look like success to curl. Every consumer would have to
 * re-implement that check, and getting it wrong fails silently in the one place
 * where silence is worst: the notification never arrives and CI is green.
 *
 * Success is decided by interpretResponse() below, which is exported and
 * covered by pushplus-notify.test.mjs.
 *
 * Exit codes
 *   0  pushed, or deliberately skipped because no token was configured
 *   1  PushPlus rejected the push, or the request could not be completed
 *   2  usage error (missing title/content, unknown template, unreadable file)
 *   3  --required was given but no token is configured
 *
 * Requires Node 18+ for global fetch.
 */

import { readFileSync, realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const PUSHPLUS_SEND_URL = "https://www.pushplus.plus/send";

/** Templates PushPlus accepts. `html` is the only one that renders rich layout. */
export const TEMPLATES = ["html", "txt", "json", "markdown"];
export const DEFAULT_TEMPLATE = "html";
export const DEFAULT_CHANNEL = "wechat";
export const DEFAULT_TIMEOUT_MS = 20_000;

const USAGE = `Usage: pushplus-notify.mjs --title <text> (--content <text> | --content-file <path>) [options]

  --title <text>          Message title. Required.
  --content <text>        Message body.
  --content-file <path>   Read the body from a file instead. Use this for large
                          or multiline bodies; passing them as argv risks hitting
                          the argument length limit.
  --template <name>       html (default) | txt | json | markdown
  --channel <name>        PushPlus channel, default wechat
  --topic <code>          Group code, for one-to-many delivery
  --to <tokens>           Friend tokens, comma separated
  --token <token>         Overrides PUSHPLUS_TOKEN. Prefer the environment so the
                          value never appears in a process listing.
  --timeout <ms>          Request timeout, default ${DEFAULT_TIMEOUT_MS}
  --required              Treat a missing token as an error (exit 3) instead of
                          skipping. Use when the push is the whole point of the
                          job; omit it for optional notifications.
  --help                  Show this text.

Reads PUSHPLUS_TOKEN from the environment when --token is not given.
Exits 0 without sending when no token is configured and --required was not given,
matching how the Slack notification steps in this repository behave.
`;

/**
 * Build the request body. Pure; exported for tests.
 *
 * Empty optional fields are omitted rather than sent as empty strings, because
 * PushPlus treats a present-but-empty `topic` as a request to deliver to a group
 * with no code instead of falling back to the token owner.
 */
export function buildPayload({
  token,
  title,
  content,
  template = DEFAULT_TEMPLATE,
  channel = DEFAULT_CHANNEL,
  topic = "",
  to = "",
}) {
  const payload = {
    token,
    title,
    content,
    template,
    channel,
  };
  if (topic) payload.topic = topic;
  if (to) payload.to = to;
  return payload;
}

/**
 * Decide whether a push actually succeeded. Pure; exported for tests.
 *
 * Two independent things must both hold: the HTTP request completed with a 2xx
 * status, and the JSON body reports `code === 200`. PushPlus returns HTTP 200
 * for rejected pushes, so the body is the authority — but a non-2xx status or an
 * unparseable body is still a failure, and must not be reported as one that
 * merely "did not contain code 200".
 */
export function interpretResponse({ httpStatus, bodyText }) {
  if (!Number.isInteger(httpStatus) || httpStatus < 200 || httpStatus >= 300) {
    return {
      ok: false,
      code: null,
      msg: "",
      reason: `HTTP ${httpStatus}`,
    };
  }

  let parsed;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    const preview = String(bodyText ?? "").slice(0, 200);
    return {
      ok: false,
      code: null,
      msg: "",
      reason: `response was not JSON (got: ${preview || "<empty>"})`,
    };
  }

  // Arrays pass `typeof === "object"`, so they need an explicit guard: an array
  // body means the endpoint answered with something other than a PushPlus
  // result object, and "code absent" would misdescribe that.
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, code: null, msg: "", reason: "response was not a JSON object" };
  }

  const code = parsed.code;
  const msg = typeof parsed.msg === "string" ? parsed.msg : "";
  if (code === 200) {
    return { ok: true, code, msg, reason: "" };
  }
  return {
    ok: false,
    code: code ?? null,
    msg,
    reason: `PushPlus code ${code ?? "absent"}${msg ? `: ${msg}` : ""}`,
  };
}

/**
 * Send one notification. Returns the interpreted response, or a failure result
 * when the request itself could not be completed — a network error must not
 * throw past the caller, which needs to print an actionable message either way.
 */
export async function sendNotification(payload, { timeout = DEFAULT_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(PUSHPLUS_SEND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const bodyText = await response.text();
    return interpretResponse({ httpStatus: response.status, bodyText });
  } catch (error) {
    const reason =
      error && error.name === "AbortError"
        ? `timed out after ${timeout}ms`
        : `request failed (${(error && error.message) || error})`;
    return { ok: false, code: null, msg: "", reason };
  } finally {
    clearTimeout(timer);
  }
}

/** Common misconfiguration causes, appended to a failure so the log is actionable. */
export const FAILURE_HINT =
  "Common causes: the token is invalid or was rotated; the WeChat official " +
  "account 'pushplus推送加' is not followed, so the wechat channel cannot " +
  "deliver; the free daily quota is exhausted; or the content exceeds the " +
  "channel's size limit.";

function parseArgs(argv) {
  const out = {
    title: "",
    content: "",
    contentFile: "",
    template: DEFAULT_TEMPLATE,
    channel: DEFAULT_CHANNEL,
    topic: "",
    to: "",
    token: "",
    timeout: DEFAULT_TIMEOUT_MS,
    required: false,
    help: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[i + 1];
      if (value === undefined) {
        throw new Error(`${arg} needs a value`);
      }
      i += 1;
      return value;
    };
    switch (arg) {
      case "--title":
        out.title = next();
        break;
      case "--content":
        out.content = next();
        break;
      case "--content-file":
        out.contentFile = next();
        break;
      case "--template":
        out.template = next();
        break;
      case "--channel":
        out.channel = next();
        break;
      case "--topic":
        out.topic = next();
        break;
      case "--to":
        out.to = next();
        break;
      case "--token":
        out.token = next();
        break;
      case "--timeout":
        out.timeout = Number.parseInt(next(), 10);
        break;
      case "--required":
        out.required = true;
        break;
      case "--help":
      case "-h":
        out.help = true;
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return out;
}

function isCurrentEntry() {
  const entry = process.argv[1];
  if (!entry) return false;
  return import.meta.url === pathToFileURL(realpathSync(entry)).href;
}

async function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`::error::${error.message}`);
    console.error(USAGE);
    return 2;
  }

  if (args.help) {
    console.log(USAGE);
    return 0;
  }

  if (!TEMPLATES.includes(args.template)) {
    console.error(
      `::error::Unknown template '${args.template}'. Expected one of: ${TEMPLATES.join(", ")}.`,
    );
    return 2;
  }
  if (!args.title.trim()) {
    console.error("::error::--title is required and must not be empty.");
    return 2;
  }

  let content = args.content;
  if (args.contentFile) {
    try {
      content = readFileSync(args.contentFile, "utf8");
    } catch (error) {
      console.error(`::error::Could not read --content-file '${args.contentFile}': ${error.message}`);
      return 2;
    }
  }
  if (!content.trim()) {
    console.error("::error::No content: pass --content or a non-empty --content-file.");
    return 2;
  }

  const token = (args.token || process.env.PUSHPLUS_TOKEN || "").trim();
  if (!token) {
    if (args.required) {
      console.error(
        "::error::PUSHPLUS_TOKEN is not set, but this step requires it. " +
          "Set it with: gh secret set PUSHPLUS_TOKEN --repo <owner>/<repo>",
      );
      return 3;
    }
    // Deliberately not a failure: notification steps elsewhere in this repo are
    // optional and no-op when unconfigured, so an absent webhook or token never
    // turns a green run red.
    console.log("::warning::PUSHPLUS_TOKEN is not set; skipping the WeChat push.");
    return 0;
  }

  const payload = buildPayload({
    token,
    title: args.title,
    content,
    template: args.template,
    channel: args.channel,
    topic: args.topic,
    to: args.to,
  });

  if (Number.isNaN(args.timeout) || args.timeout <= 0) {
    console.error(`::error::--timeout must be a positive integer, got '${args.timeout}'.`);
    return 2;
  }

  // Never log the payload: it contains the token.
  console.log(
    `Posting to PushPlus (channel=${payload.channel}, template=${payload.template}, ` +
      `${Buffer.byteLength(content, "utf8")} bytes of content)…`,
  );

  const result = await sendNotification(payload, { timeout: args.timeout });
  if (result.ok) {
    console.log(`✅ Push accepted (code 200${result.msg ? `: ${result.msg}` : ""}).`);
    return 0;
  }

  console.error(`::error::WeChat push failed — ${result.reason}`);
  console.error(`   ${FAILURE_HINT}`);
  return 1;
}

if (isCurrentEntry()) {
  main()
    .then((code) => process.exit(code))
    .catch((error) => {
      console.error(`::error::${error.name || "Error"}: ${error.message}`);
      process.exit(2);
    });
}
