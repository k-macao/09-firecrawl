import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

import {
  buildPayload,
  interpretResponse,
  sendNotification,
  DEFAULT_TEMPLATE,
  DEFAULT_CHANNEL,
  TEMPLATES,
} from "./pushplus-notify.mjs";

const run = promisify(execFile);
const SCRIPT = fileURLToPath(new URL("./pushplus-notify.mjs", import.meta.url));

/** Run the CLI as a subprocess, capturing its exit code without throwing. */
async function cli(args, env = {}) {
  try {
    const { stdout, stderr } = await run(process.execPath, [SCRIPT, ...args], {
      // PUSHPLUS_TOKEN is cleared unless the test sets it, so no test can
      // accidentally reach the network with a real token from the environment.
      env: { ...process.env, PUSHPLUS_TOKEN: "", ...env },
    });
    return { code: 0, stdout, stderr };
  } catch (error) {
    return { code: error.code, stdout: error.stdout ?? "", stderr: error.stderr ?? "" };
  }
}

/** Run a callback with globalThis.fetch replaced, restoring it afterwards. */
async function withFetchStub(stub, callback) {
  const original = globalThis.fetch;
  globalThis.fetch = stub;
  try {
    return await callback();
  } finally {
    globalThis.fetch = original;
  }
}

test("buildPayload keeps required fields and drops empty optional ones", (t) => {
  const minimal = buildPayload({ token: "tk", title: "T", content: "C" });
  t.assert.deepEqual(minimal, {
    token: "tk",
    title: "T",
    content: "C",
    template: DEFAULT_TEMPLATE,
    channel: DEFAULT_CHANNEL,
  });
  // An empty `topic` must be omitted, not sent as "": PushPlus reads a present
  // but empty topic as "deliver to a group with no code" rather than falling
  // back to the token owner.
  t.assert.equal("topic" in minimal, false);
  t.assert.equal("to" in minimal, false);

  const full = buildPayload({
    token: "tk",
    title: "T",
    content: "C",
    template: "markdown",
    channel: "wechat",
    topic: "grp",
    to: "a,b",
  });
  t.assert.deepEqual(full, {
    token: "tk",
    title: "T",
    content: "C",
    template: "markdown",
    channel: "wechat",
    topic: "grp",
    to: "a,b",
  });
});

test("interpretResponse treats body code 200 as success", (t) => {
  const result = interpretResponse({
    httpStatus: 200,
    bodyText: JSON.stringify({ code: 200, msg: "请求成功", data: "12345" }),
  });
  t.assert.equal(result.ok, true);
  t.assert.equal(result.code, 200);
  t.assert.equal(result.msg, "请求成功");
});

test("interpretResponse rejects an HTTP 200 whose body code is not 200", (t) => {
  // This is the case that makes the script worth having: PushPlus answers
  // HTTP 200 for a rejected push, so `curl --fail` would report success while
  // nothing was ever delivered.
  const result = interpretResponse({
    httpStatus: 200,
    bodyText: JSON.stringify({ code: 903, msg: "token无效" }),
  });
  t.assert.equal(result.ok, false);
  t.assert.equal(result.code, 903);
  t.assert.match(result.reason, /PushPlus code 903/);
  t.assert.match(result.reason, /token无效/);
});

test("interpretResponse reports a missing code as a failure", (t) => {
  const result = interpretResponse({ httpStatus: 200, bodyText: JSON.stringify({ msg: "hi" }) });
  t.assert.equal(result.ok, false);
  t.assert.equal(result.code, null);
  t.assert.match(result.reason, /code absent/);
});

test("interpretResponse distinguishes an HTTP failure from a body failure", (t) => {
  for (const httpStatus of [400, 401, 403, 429, 500, 502, 503]) {
    const result = interpretResponse({
      httpStatus,
      bodyText: JSON.stringify({ code: 200, msg: "请求成功" }),
    });
    t.assert.equal(result.ok, false, `HTTP ${httpStatus} must not count as success`);
    t.assert.match(result.reason, new RegExp(`^HTTP ${httpStatus}$`));
  }
});

test("interpretResponse survives a non-JSON and a non-object body", (t) => {
  const html = interpretResponse({
    httpStatus: 200,
    bodyText: "<html><body>502 Bad Gateway</body></html>",
  });
  t.assert.equal(html.ok, false);
  t.assert.match(html.reason, /not JSON/);
  t.assert.match(html.reason, /502 Bad Gateway/, "the preview should help diagnose the failure");

  const empty = interpretResponse({ httpStatus: 200, bodyText: "" });
  t.assert.equal(empty.ok, false);
  t.assert.match(empty.reason, /<empty>/);

  const array = interpretResponse({ httpStatus: 200, bodyText: "[1,2,3]" });
  t.assert.equal(array.ok, false);
  t.assert.match(array.reason, /not a JSON object/);
});

test("sendNotification returns a failure instead of throwing on a network error", async (t) => {
  const result = await withFetchStub(
    () => {
      throw new TypeError("fetch failed");
    },
    () => sendNotification({ token: "tk", title: "T", content: "C" }),
  );
  t.assert.equal(result.ok, false);
  t.assert.match(result.reason, /request failed/);
});

test("sendNotification reports a timeout distinctly", async (t) => {
  const abortError = new Error("This operation was aborted");
  abortError.name = "AbortError";
  const result = await withFetchStub(
    () => {
      throw abortError;
    },
    () => sendNotification({ token: "tk", title: "T", content: "C" }, { timeout: 5 }),
  );
  t.assert.equal(result.ok, false);
  t.assert.match(result.reason, /timed out after 5ms/);
});

test("sendNotification posts JSON and reads the reply body", async (t) => {
  let seen = null;
  const result = await withFetchStub(async (url, init) => {
    seen = { url, init };
    return { status: 200, text: async () => JSON.stringify({ code: 200, msg: "ok" }) };
  }, () => sendNotification({ token: "tk", title: "T", content: "C" }));

  t.assert.equal(result.ok, true);
  t.assert.equal(seen.url, "https://www.pushplus.plus/send");
  t.assert.equal(seen.init.method, "POST");
  t.assert.equal(seen.init.headers["Content-Type"], "application/json");
  t.assert.deepEqual(JSON.parse(seen.init.body), { token: "tk", title: "T", content: "C" });
  t.assert.ok(seen.init.signal instanceof AbortSignal, "a timeout signal must be attached");
});

test("TEMPLATES lists every template PushPlus accepts", (t) => {
  t.assert.deepEqual(TEMPLATES, ["html", "txt", "json", "markdown"]);
  t.assert.equal(DEFAULT_TEMPLATE, "html");
});

// ---- CLI contract -----------------------------------------------------------
// The exit code is what a workflow step branches on, so it is asserted by
// running the script as a subprocess. None of these cases reach the network:
// each one fails or skips before the request is built.

test("cli: --help exits 0 and prints usage", async (t) => {
  const { code, stdout } = await cli(["--help"]);
  t.assert.equal(code, 0);
  t.assert.match(stdout, /Usage: pushplus-notify\.mjs/);
});

test("cli: an absent token skips the push with exit 0", async (t) => {
  const { code, stdout } = await cli(["--title", "T", "--content", "C"]);
  t.assert.equal(code, 0);
  t.assert.match(stdout, /::warning::PUSHPLUS_TOKEN is not set/);
});

test("cli: --required turns an absent token into exit 3", async (t) => {
  const { code, stderr } = await cli(["--title", "T", "--content", "C", "--required"]);
  t.assert.equal(code, 3);
  t.assert.match(stderr, /::error::PUSHPLUS_TOKEN is not set/);
  t.assert.match(stderr, /gh secret set PUSHPLUS_TOKEN/, "the message should say how to fix it");
});

test("cli: argument validation exits 2", async (t) => {
  const cases = [
    { args: ["--content", "C"], label: "missing title" },
    { args: ["--title", "T"], label: "missing content" },
    { args: ["--title", "  ", "--content", "C"], label: "blank title" },
    { args: ["--title", "T", "--content", "   "], label: "blank content" },
    {
      args: ["--title", "T", "--content", "C", "--template", "pdf"],
      label: "unknown template",
    },
    { args: ["--title", "T", "--content-file", "/nonexistent/nope.html"], label: "bad file" },
    { args: ["--bogus"], label: "unknown flag" },
    { args: ["--title"], label: "flag with no value" },
  ];
  for (const { args, label } of cases) {
    const { code, stderr } = await cli(args);
    t.assert.equal(code, 2, `${label} should exit 2, got ${code}`);
    t.assert.match(stderr, /::error::/, `${label} should emit an annotation`);
  }
});

test("cli: an unknown template lists the valid ones", async (t) => {
  const { stderr } = await cli(["--title", "T", "--content", "C", "--template", "pdf"]);
  t.assert.match(stderr, /html, txt, json, markdown/);
});

test("cli: an invalid --timeout is rejected before any request", async (t) => {
  // With a token present this would otherwise reach the network, so the
  // validation is asserted on a non-positive value which cannot be used.
  const { code, stderr } = await cli(
    ["--title", "T", "--content", "C", "--timeout", "0"],
    { PUSHPLUS_TOKEN: "tk" },
  );
  t.assert.equal(code, 2);
  t.assert.match(stderr, /--timeout must be a positive integer/);
});
