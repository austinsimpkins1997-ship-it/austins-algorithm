"use strict";
// Run a fresh, offline Chromium instance without the user's browser profile.
const assert = require("node:assert/strict");
const {spawn} = require("node:child_process");
const fs = require("node:fs/promises");
const {existsSync} = require("node:fs");
const path = require("node:path");
const {setTimeout: delay} = require("node:timers/promises");
const {pathToFileURL} = require("node:url");

const root = path.join(__dirname, "..");
const build = path.join(root, ".build");
const browser = [
  process.env.FUTURE_READER_BROWSER,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/chromium",
  "/usr/bin/google-chrome",
].find(file => file && existsSync(file));

async function waitFor(check, description) {
  for (let attempt = 0; attempt < 100; attempt++) {
    const result = await check();
    if (result) return result;
    await delay(100);
  }
  throw new Error("Timed out waiting for " + description);
}

async function channel(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, {once: true});
    socket.addEventListener("error", reject, {once: true});
  });
  let id = 0;
  const pending = new Map();
  const events = [];
  socket.addEventListener("message", ({data}) => {
    const response = JSON.parse(data);
    if (response.id === undefined) {
      events.push(response);
      return;
    }
    const request = pending.get(response.id);
    if (!request) return;
    pending.delete(response.id);
    clearTimeout(request.timeout);
    if (response.error) request.reject(new Error(response.error.message));
    else request.resolve(response.result);
  });
  return {
    socket, events,
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const current = ++id;
        const timeout = setTimeout(() => {
          pending.delete(current);
          reject(new Error("Browser did not answer " + method));
        }, 8000);
        pending.set(current, {resolve, reject, timeout});
        socket.send(JSON.stringify({id: current, method, params}));
      });
    },
  };
}

async function main() {
  assert.ok(browser, "Set FUTURE_READER_BROWSER to a Chromium executable.");
  await fs.mkdir(build, {recursive: true});
  const scratch = await fs.mkdtemp(path.join(build, "browser-"));
  const profile = path.join(scratch, "profile");
  const downloads = path.join(scratch, "downloads");
  await fs.mkdir(downloads);
  const app = spawn(browser, [
    "--headless=new", "--disable-background-networking", "--disable-component-update",
    "--disable-sync", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0",
    "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + profile, "about:blank",
  ], {windowsHide: true, stdio: "ignore"});
  let connection;
  try {
    const port = await waitFor(async () => {
      if (app.exitCode !== null) throw new Error("Chromium exited with status " + app.exitCode);
      try { return (await fs.readFile(path.join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0].trim(); }
      catch (error) { if (error.code !== "ENOENT") throw error; return null; }
    }, "the isolated browser's DevTools port");
    const targets = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
    connection = await channel(targets.find(target => target.type === "page").webSocketDebuggerUrl);
    const {send, events} = connection;
    async function evaluate(expression) {
      const response = await send("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true});
      if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description ||
        response.exceptionDetails.text);
      return response.result.value;
    }
    const click = id => evaluate("document.getElementById(" + JSON.stringify(id) + ").click()");
    const read = id => evaluate("document.getElementById(" + JSON.stringify(id) + ").textContent");
    const setInput = (id, value) => evaluate("(() => { const element = document.getElementById(" + JSON.stringify(id) +
      "); element.value = " + JSON.stringify(value) + "; element.dispatchEvent(new Event('input', {bubbles:true})); })()");
    const show = view => evaluate("document.querySelector('[data-view=" + view + "]').click()");
    const choose = (id, value) => evaluate("(() => { const element = document.getElementById(" + JSON.stringify(id) +
      "); element.value = " + JSON.stringify(value) + "; element.dispatchEvent(new Event('change', {bubbles:true})); })()");
    const key = async name => {
      const code = {Enter: 13, Tab: 9}[name];
      await send("Input.dispatchKeyEvent", {type: "keyDown", key: name, code: name,
        text: name === "Enter" ? "\r" : "", windowsVirtualKeyCode: code, nativeVirtualKeyCode: code});
      await send("Input.dispatchKeyEvent", {type: "keyUp", key: name, code: name,
        windowsVirtualKeyCode: code, nativeVirtualKeyCode: code});
    };
    const download = async (id, name) => {
      await click(id);
      const file = path.join(downloads, name);
      await waitFor(() => existsSync(file), name);
      return fs.readFile(file);
    };
    await send("Page.enable");
    await send("Runtime.enable");
    await send("Network.enable");
    await send("Network.emulateNetworkConditions", {offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0});
    await send("Browser.setDownloadBehavior", {behavior: "allow", downloadPath: downloads});
    await send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
    await send("Emulation.setEmulatedMedia", {features: [{name: "prefers-reduced-motion", value: "reduce"}]});
    const url = pathToFileURL(path.join(root, "future-reader.html")).href;
    await send("Page.navigate", {url});
    await waitFor(async () => {
      if (await evaluate("document.readyState") !== "complete") return false;
      return evaluate("document.querySelector('#current')?.textContent.includes('Start')");
    }, "the offline practice view");
    assert.equal(await evaluate("location.protocol"), "file:");
    assert.equal(await evaluate("document.documentElement.scrollWidth <= innerWidth"), true);
    await click("guide");
    assert.equal(await evaluate("document.activeElement.id"), "tile-B");
    await evaluate("document.getElementById('tile-G').focus()");
    await key("Enter");
    await waitFor(async () => /Shelf.*Goal/.test(await read("pieces")), "keyboard placement of the Goal piece");
    await click("travel");
    assert.match(await read("preview-path"), /Start → Shelf → Goal/);
    assert.equal(await evaluate("document.activeElement.id"), "confirm");
    await show("advanced");
    assert.match(await read("program"), /future-reader\.program\/1/);
    assert.match(await read("window-detail"), /second 5/);
    assert.match(await read("model-choice"), /Wait for the check leads on expected value.*Use the fallback leads on worst-case value/);
    assert.match(await read("model-threshold"), /75%/);
    const decision = await download("download-decision", "future-reader-decision-example.json");
    assert.deepEqual(JSON.parse(decision), JSON.parse(await fs.readFile(path.join(root, "fixtures", "decisions.json"), "utf8")));
    await show("practice");
    await evaluate("document.getElementById('confirm').focus()");
    await key("Enter");
    await waitFor(async () => /reached the goal/.test(await read("current")), "keyboard confirmation");
    await click("check");
    assert.match(await read("certificate"), /Verified.*goal/);
    await click("reverse");
    await show("advanced");
    assert.match(await read("program-note"), /restores a checkpoint/);
    assert.equal(JSON.parse(await read("program")).steps.at(-1).state, "G");
    await show("practice");
    await click("confirm");
    assert.match(await read("current"), /at Start/);
    assert.equal(await read("clock-badge"), "Clock 8 s");
    await show("handoff");
    const saved = await download("export", "future-reader-checkpoint.json");
    const checkpoint = JSON.parse(saved);
    assert.deepEqual(checkpoint.events.map(row => row.kind), ["travel", "reverse"]);
    const prompt = await download("download-prompt", "future-reader-master-prompt.txt");
    assert.deepEqual(prompt, await fs.readFile(path.join(root, "MASTER_PROMPT.txt")));
    for (const name of ["opportunity", "runtime"]) {
      await choose("prompt-version", name);
      const supplement = await download("download-prompt", "future-reader-" + name + "-brief.txt");
      assert.deepEqual(supplement, await fs.readFile(path.join(root, "fixtures", "brief-" + name + ".txt")));
      assert.equal(await read("prompt"), supplement.toString("utf8"));
    }
    const portable = await download("export-record", "future-reader-record.json");
    assert.equal(JSON.parse(portable).temporal.schema_version, 1);
    assert.deepEqual(JSON.parse(portable).decisions, JSON.parse(decision));
    assert.equal(Object.keys(JSON.parse(portable).supplements).length, 2);
    await show("records");
    await choose("temporal-select", "as_known_then");
    assert.match(await read("temporal-output"), /As known then/i);
    await choose("case-select", "cern");
    await choose("case-operation", "CERTIFY");
    assert.match(await read("case-output"), /153|4\.58|equivalen/i);
    await choose("case-select", "cenomanian");
    assert.match(await read("case-output"), /coastal|Cenomanian/i);
    await show("advanced");
    await setInput("new-clock", "9");
    await evaluate("document.getElementById('revision-form').requestSubmit()");
    assert.match(await read("revision-diff"), /8 → 9/);
    await click("apply-revision");
    assert.match(await read("trace"), /Revision · 9 s/);
    await show("handoff");
    const {root: dom} = await send("DOM.getDocument");
    const {nodeId} = await send("DOM.querySelector", {nodeId: dom.nodeId, selector: "#import"});
    await send("DOM.setFileInputFiles", {nodeId, files: [path.join(downloads, "future-reader-record.json")]});
    await waitFor(async () => (await read("import-status")).includes("verified by replay"), "checkpoint validation");
    await click("restore-import");
    await show("practice");
    assert.equal(await read("clock-badge"), "Clock 8 s");
    await show("handoff");
    assert.match(await read("release-summary"), /0\.1\.0 → 0\.1\.1/);
    await click("return-branch");
    await show("practice");
    assert.equal(await read("clock-badge"), "Clock 9 s");
    await show("handoff");
    await click("return-branch");
    await show("practice");
    assert.equal(await read("clock-badge"), "Clock 8 s");

    await send("Page.reload");
    await waitFor(async () => (await read("clock-badge")) === "Clock 0 s", "a fresh practice session");
    await click("travel");
    await show("advanced");
    await setInput("new-clock", "6");
    await evaluate("document.getElementById('revision-form').requestSubmit()");
    assert.match(await read("revision-diff"), /Start → Bench → Goal/);
    await click("apply-revision");
    await show("practice");
    assert.equal(await evaluate("document.getElementById('preview-card').hidden"), true);
    await click("travel");
    assert.match(await read("preview-path"), /Start → Bench → Goal/);
    await send("Accessibility.enable");
    const tree = await send("Accessibility.getFullAXTree");
    const named = new Set(tree.nodes.filter(node => !node.ignored).map(node => node.name?.value?.trim()));
    for (const label of ["Pinpoint", "Travel", "Reverse", "Check", "What’s happening?"]) {
      assert.ok(named.has(label), label + " absent from the Chromium accessibility tree: " + [...named].slice(0, 100).join(" | "));
    }
    assert.deepEqual(events.filter(event => event.method === "Runtime.exceptionThrown"), []);
    assert.deepEqual(events.filter(event => event.method === "Network.requestWillBeSent" &&
      /^https?:/.test(event.params.request.url)).map(event => event.params.request.url), []);
    await evaluate("window.scrollTo(0, 0)");
    const {cssContentSize} = await send("Page.getLayoutMetrics");
    const {data} = await send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true,
      clip: {x: 0, y: 0, width: cssContentSize.width, height: cssContentSize.height, scale: 1}});
    await fs.writeFile(path.join(scratch, "practice.png"), Buffer.from(data, "base64"));
    const report = {status: "PASS", browser, protocol: "file:", viewport: [390, 844], reducedMotion: true,
      network: "offline", keyboardMove: true, originalPromptBytes: prompt.length,
      checkpointReplay: true, revisionInvalidatedPreview: true, accessibilityNames: true,
      temporalAndReconstructionCases: true, briefDownloads: 3, decisionExample: true};
    await fs.writeFile(path.join(scratch, "report.json"), JSON.stringify(report, null, 2) + "\n");
    console.log(JSON.stringify({...report, artifacts: scratch}));
  } finally {
    if (connection) connection.socket.close();
    app.kill();
  }
}

main().catch(error => {
  console.error(error.stack);
  process.exitCode = 1;
});
