// Run with: node --test tests/test_browser_auth.cjs
// These exercise UI auth handling without changing the design or requiring a live IdP.
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function browser(response) {
  const calls = [], alerts = [], sources = [];
  class EventSource {
    constructor() { this.handlers = {}; this.closed = false; sources.push(this); }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    close() { this.closed = true; }
  }
  const context = vm.createContext({
    document: { querySelector() {} },
    fetch: async (url, options) => { calls.push({ url, options }); return response; },
    EventSource, alert: (message) => alerts.push(message),
  });
  const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
  vm.runInContext(source.split("\ninit().catch(")[0] +
    "\nglobalThis.api = {apiFetch, streamTrace};", context);
  return { ...context.api, calls, alerts, sources };
}

test("same-origin fetch retains protection header and browser credentials", async () => {
  const b = browser({ status: 200, headers: { get: () => "application/json" } });
  await b.apiFetch("/api/audit", { method: "POST", headers: { "Custom": "value" } });
  assert.equal(b.calls[0].options.credentials, "same-origin");
  assert.equal(b.calls[0].options.headers["X-ClaimShield-Request"], "1");
  assert.equal(b.calls[0].options.headers.Custom, "value");
});

for (const failure of [401, 403, "redirect", "login-html"]) {
  test(`auth failure ${failure} closes the stream and requests sign-in`, async () => {
    const b = browser({ status: typeof failure === "number" ? failure : 200,
      redirected: failure === "redirect",
      headers: { get: () => failure === "login-html" ? "text/html" : "application/json" } });
    b.streamTrace("case-example");
    await assert.rejects(b.apiFetch("/api/cases/case-example"), /Reload this page/);
    assert.equal(b.sources[0].closed, true);
  });
}

test("server expiry event closes SSE and asks for a page reload", () => {
  const b = browser({});
  b.streamTrace("case-example");
  b.sources[0].handlers.auth_expired();
  assert.equal(b.sources[0].closed, true);
  assert.match(b.alerts[0], /expired/);
});

test("SSE retry stops when the case is unavailable", async () => {
  const b = browser({ status: 404, ok: false, headers: { get: () => "application/json" } });
  b.streamTrace("case-example");
  b.sources[0].onerror();
  await new Promise(setImmediate);
  assert.equal(b.sources[0].closed, true);
  assert.match(b.alerts[0], /unavailable/);
});
