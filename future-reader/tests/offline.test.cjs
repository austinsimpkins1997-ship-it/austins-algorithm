"use strict";
const assert = require("node:assert/strict");
const {createHash} = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const core = require("../tools/companion/app.js");

const root = path.join(__dirname, "..");
const text = name => fs.readFileSync(path.join(root, name), "utf8");
const fixture = JSON.parse(text("fixtures/operational.json"));
const {domain, session} = fixture;

test("all route alternatives match the evaluated Python fixture", () => {
  assert.ok(fixture.routes.length >= 9);
  for (const {start, goal, now, check_available, result} of fixture.routes) {
    assert.deepEqual(core.routes(domain, start, goal, now, check_available), result);
  }
});

test("placed moves compile to the Python program and travel and reverse preserve its states", () => {
  const route = session.program_route;
  assert.deepEqual(core.compile(route.path), session.program);
  const initial = core.newSession();
  const traveled = core.transition(domain, initial, {kind: "travel", path: route.path});
  const reversed = core.transition(domain, traveled, {kind: "reverse"});
  const project = ({state, now, revision}) => ({state, now, revision});
  assert.deepEqual(project(initial), session.initial);
  assert.deepEqual(project(traveled), session.travel);
  assert.deepEqual(project(reversed), session.reverse);
  assert.equal(reversed.events.length, 2);
  assert.equal(traveled.events.length, 1);
  assert.deepEqual(core.restore(domain, core.checkpoint(domain, reversed)), reversed);
  assert.deepEqual(core.restore(domain, {schema: "future-reader.browser-record/1",
    checkpoint: core.checkpoint(domain, reversed), evidence: "<img src=invalid>"}), reversed);
  assert.equal(core.routes(domain, "S", "G", 0, true)[0].cost, session.certificate.cost);
});

test("replay rejects stale geometry, disconnected edges, failed checks, and backwards clocks", () => {
  const initial = core.newSession();
  const checkpoint = core.checkpoint(domain, initial);
  assert.throws(() => core.restore(domain, {...checkpoint, model: "other"}), /incompatible/);
  assert.throws(() => core.restore(domain, {...checkpoint, events: [{kind: "travel", path: ["S", "G"]}]}), /disconnected/);
  assert.throws(() => core.restore(domain, {...checkpoint, events: [{kind: "reverse"}]}), /checkpoint/);
  assert.throws(() => core.restore(domain, {...checkpoint, events: [{kind: "revision", now: 1, check: 1}]}), /valid check/);
  const revised = core.transition(domain, initial, {kind: "revision", now: 6, check: false});
  assert.throws(() => core.transition(domain, revised, {kind: "revision", now: 4, check: true}), /current clock/);
  assert.throws(() => core.transition(domain, revised, {kind: "travel", path: ["S", "A", "G"]}), /independent check/);
  assert.throws(() => core.transition(domain, revised, {kind: "travel", path: ["S", "B", "G"]}), /deadline/);
  assert.throws(() => core.restore(domain, JSON.parse('{"schema":"future-reader.browser-checkpoint/1","__proto__":{"polluted":true}}')), /incompatible/);
  assert.equal({}.polluted, undefined);
});

test("single file carries exact prompt bytes and evaluated fixtures", () => {
  const html = text("future-reader.html");
  const embedded = html.match(/<script id="companion-data" type="application\/json">([^<]+)<\/script>/);
  assert.ok(embedded);
  const data = JSON.parse(embedded[1]);
  assert.deepEqual(Buffer.from(data.prompt, "utf8"), fs.readFileSync(path.join(root, "MASTER_PROMPT.txt")));
  assert.equal(data.prompt_sha256, createHash("sha256").update(data.prompt).digest("hex"));
  for (const name of ["operational", "temporal", "evidence", "release-preview", "decisions"]) {
    assert.deepEqual(data[name], JSON.parse(text("fixtures/" + name + ".json")));
  }
  for (const name of ["opportunity", "runtime"]) {
    const original = fs.readFileSync(path.join(root, "fixtures", "brief-" + name + ".txt"));
    assert.deepEqual(Buffer.from(data.supplements[name].text, "utf8"), original);
    assert.equal(data.supplements[name].sha256, createHash("sha256").update(original).digest("hex"));
  }
  assert.doesNotMatch(html, /<(?:script|link|img)\b[^>]*\b(?:src|href)\s*=/i);
  assert.match(html, /default-src 'none'/);
});
