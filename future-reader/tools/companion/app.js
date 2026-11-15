"use strict";
const Companion = (() => {
  const PROGRAM = "future-reader.program/1";
  const CHECKPOINT = "future-reader.browser-checkpoint/1";
  const RECORD = "future-reader.browser-record/1";
  const MAX_EVENTS = 200;
  const MAX_IMPORT_BYTES = 262144;

  function requireThat(condition, message) {
    if (!condition) throw new Error(message);
  }

  function exactKeys(value, fields) {
    return value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).length === fields.length && fields.every(key => Object.hasOwn(value, key));
  }

  function routes(domain, start, goal, now, checkAvailable) {
    const results = [];
    const reserve = domain.verify + domain.margin + domain.uncertainty;
    function walk(path, edges, arrival, cost, checkNeeded) {
      const point = path.at(-1);
      if (point === goal) {
        const duration = edges.reduce((sum, edge) => sum + edge.duration, 0);
        const latest = domain.deadline - reserve - duration - (domain.equality ? 0 : 1);
        const latestArrival = edges.reduce((t, edge) => Math.max(t, edge.opens) + edge.duration, latest);
        const feasible = t => domain.equality ? t + reserve <= domain.deadline : t + reserve < domain.deadline;
        const reason = checkNeeded && !checkAvailable ? "PREREQUISITE_UNAVAILABLE" :
          feasible(arrival) ? null : "DEADLINE";
        results.push({path, departure: now, arrival, cost,
          latest_safe_start: latest >= 0 && feasible(latestArrival) ? latest : null,
          eligible: reason === null, reason});
        return;
      }
      for (const edge of domain.edges) {
        if (edge.source === point && !path.includes(edge.target)) {
          walk([...path, edge.target], [...edges, edge], Math.max(arrival, edge.opens) + edge.duration,
            cost + edge.cost, checkNeeded || edge.requires_check);
        }
      }
    }
    walk([start], [], now, 0, false);
    return results.sort((a, b) => a.cost - b.cost || a.arrival - b.arrival ||
      a.path.join("").localeCompare(b.path.join("")));
  }

  function newSession() {
    return {state: "S", now: 0, check: true, revision: 0, events: [], checkpoints: []};
  }

  function validateContext(now, check, priorClock) {
    requireThat(Number.isSafeInteger(now) && now >= priorClock && now <= 3600 && typeof check === "boolean",
      "Choose a whole simulated second from the current clock to 3600 and a valid check setting.");
  }

  function moveRoute(domain, session, path) {
    requireThat(Array.isArray(path) && path.length >= 2 && path.length <= Object.keys(domain.nodes).length &&
      path.every(node => typeof node === "string" && Object.hasOwn(domain.nodes, node)) &&
      path[0] === session.state && new Set(path).size === path.length, "Choose connected moves from your current position.");
    const route = routes(domain, session.state, path.at(-1), session.now, session.check)
      .find(row => row.path.length === path.length && row.path.every((node, index) => path[index] === node));
    requireThat(route, "That path has a disconnected move.");
    requireThat(route.eligible, route.reason === "DEADLINE" ?
      "That move misses the deadline and its reserved time." : "That move needs the independent check.");
    return route;
  }

  function transition(domain, session, event) {
    requireThat(session.events.length < MAX_EVENTS, "The practice history is full. Download it and open a fresh companion.");
    let next;
    if (exactKeys(event, ["kind", "path"]) && event.kind === "travel") {
      const route = moveRoute(domain, session, event.path);
      const checkpoints = [...session.checkpoints, session.state];
      next = {state: route.path.at(-1), now: route.arrival, check: session.check, checkpoints};
    } else if (exactKeys(event, ["kind"]) && event.kind === "reverse") {
      requireThat(session.checkpoints.length, "Travel first to create a checkpoint to reverse.");
      next = {state: session.checkpoints.at(-1), now: session.now, check: session.check,
        checkpoints: session.checkpoints.slice(0, -1)};
    } else if (exactKeys(event, ["kind", "now", "check"]) && event.kind === "revision") {
      validateContext(event.now, event.check, session.now);
      requireThat(event.now !== session.now || event.check !== session.check, "Change the clock or the check before applying.");
      next = {state: session.state, now: event.now, check: event.check, checkpoints: session.checkpoints};
    } else {
      throw new Error("Unknown or malformed practice event.");
    }
    return {...next, revision: session.revision + 1, events: [...session.events, event]};
  }

  function checkpoint(domain, session) {
    return {schema: CHECKPOINT, domain: domain.id, model: JSON.stringify(domain), events: session.events};
  }

  function restore(domain, source) {
    if (source?.schema === RECORD) {
      requireThat(Object.hasOwn(source, "checkpoint"), "The record has no practice checkpoint.");
      source = source.checkpoint;
    }
    requireThat(exactKeys(source, ["schema", "domain", "model", "events"]) &&
      source.schema === CHECKPOINT && source.domain === domain.id && source.model === JSON.stringify(domain) &&
      Array.isArray(source.events) && source.events.length <= MAX_EVENTS,
      "This checkpoint is incompatible with the embedded practice model.");
    return source.events.reduce((session, event) => transition(domain, session, event), newSession());
  }

  function compile(path) {
    return {schema: PROGRAM, steps: [
      {op: "Start", state: path[0]},
      ...path.slice(1).map(to => ({op: "Move", to})),
      {op: "Goal", state: path.at(-1)},
    ]};
  }

  function mount(document, data) {
    const domain = data.operational.domain;
    const byId = id => document.getElementById(id);
    const set = (id, value) => { byId(id).textContent = value; };
    const button = (id, handler) => byId(id).addEventListener("click", run(handler));
    const keys = Object.keys(domain.nodes);
    const tileSymbols = {S: "⌂", A: "□", B: "△", G: "⚑"};
    const views = [...document.querySelectorAll("[data-view]")];
    const labels = path => path.map(node => domain.nodes[node]).join(" → ");
    const briefs = {
      master: {title: "Master prompt · original", filename: "future-reader-master-prompt.txt",
        text: data.prompt, sha256: data.prompt_sha256},
      ...data.supplements,
    };
    const tile = {};
    let session = newSession();
    let pieces = [];
    let preview = null;
    let proposedRevision = null;
    let imported = null;
    let alternate = null;
    let certificate = null;

    function say(message, speak = true) {
      set("feedback", message);
      if (speak && byId("sound").checked && "speechSynthesis" in window &&
          typeof window.SpeechSynthesisUtterance === "function") {
        const voice = window.speechSynthesis.getVoices().find(item => item.localService && item.lang.startsWith("en"));
        if (voice) {
          const cue = new window.SpeechSynthesisUtterance(message);
          cue.voice = voice;
          window.speechSynthesis.cancel();
          window.speechSynthesis.speak(cue);
        }
      }
    }

    function run(handler) {
      return (...args) => {
        try { handler(...args); } catch (error) { say(error.message); }
      };
    }

    function make(tag, value) {
      const element = document.createElement(tag);
      if (value !== undefined) element.textContent = value;
      return element;
    }

    function details(parent, label, value) {
      const box = make("details");
      box.append(make("summary", label), make("pre", JSON.stringify(value, null, 2)));
      parent.append(box);
    }

    function safeLink(parent, source) {
      const title = source.title || source.id || "Source";
      if (source.url) {
        try {
          const url = new URL(source.url);
          if (url.protocol === "https:" || url.protocol === "http:") {
            const link = make("a", title);
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            parent.append(link);
            return;
          }
        } catch { /* Keep malformed sources visible as text. */ }
      }
      parent.append(make("span", title));
    }

    function selectValues(id, entries) {
      byId(id).replaceChildren(...entries.map(([key, label]) => {
        const option = make("option", label);
        option.value = key;
        return option;
      }));
    }

    function selectedPath() {
      return [session.state, ...pieces];
    }

    function selectMove(node) {
      if (node === pieces.at(-1)) {
        pieces.pop();
        say("Piece removed.");
      } else {
        const endpoint = selectedPath().at(-1);
        requireThat(!selectedPath().includes(node) &&
          domain.edges.some(edge => edge.source === endpoint && edge.target === node),
          "Pick a connected tile marked with a plus.");
        pieces.push(node);
        say("Placed " + domain.nodes[node] + ".");
      }
      preview = null;
      render();
    }

    function bestRoutes() {
      return routes(domain, session.state, "G", session.now, session.check);
    }

    function routeText(route) {
      return route.eligible ? "Eligible" : route.reason === "DEADLINE" ? "Deadline" : "Check required";
    }

    function opportunity(routeRows) {
      const best = routeRows.find(route => route.eligible);
      const reserve = domain.verify + domain.margin + domain.uncertainty;
      set("decision", best ? labels(best.path) + " is the lowest-cost eligible route." :
        "The current inputs leave no eligible route to the goal.");
      if (session.state === "G") {
        set("window-summary", "The practice position is already at the goal.");
      } else if (best) {
        const remaining = best.latest_safe_start - session.now;
        set("window-summary", remaining ? "You can start the least-cost route through second " + best.latest_safe_start + "." :
          "Now is the last safe start for the least-cost route.");
      } else {
        set("window-summary", "The route window is closed or a prerequisite is missing.");
      }
      const starts = routeRows.filter(route => route.reason !== "PREREQUISITE_UNAVAILABLE" && route.latest_safe_start !== null)
        .filter((route, index, candidates) => index === 0 || route.latest_safe_start > candidates[index - 1].latest_safe_start);
      const transition = starts.length > 1 ? " After second " + starts[0].latest_safe_start + ", the least-cost route changes to " +
        labels(starts[1].path) + "." : "";
      set("window-detail", "Deadline " + domain.deadline + " s, inclusive. Reserve " + reserve + " s for verification, margin, and clock uncertainty." +
        transition + (starts.length && session.state !== "G" ?
          " After second " + Math.max(...starts.map(route => route.latest_safe_start)) + ", no route fits." : ""));
      byId("routes").replaceChildren(...routeRows.map(route => {
        const row = make("tr");
        for (const value of [labels(route.path), route.cost, route.arrival + " s",
          route.latest_safe_start === null ? "Unavailable" : route.latest_safe_start + " s", routeText(route)]) {
          row.append(make("td", String(value)));
        }
        return row;
      }));
    }

    function render() {
      const routeRows = bestRoutes();
      const selected = selectedPath();
      const endpoint = selected.at(-1);
      set("clock-badge", "Clock " + session.now + " s");
      set("current", session.state === "G" ? "You reached the goal." : "You are at " + domain.nodes[session.state] + ".");
      set("next", preview ? "Review the preview, then move when ready." : session.state === "G" ?
        "Check the result or reverse to the earlier position." : pieces.length ?
          "Add another connected piece or select Travel to preview." : "Choose a connected tile to build a route, or let Travel find one.");
      for (const node of keys) {
        const entry = tile[node];
        const current = node === session.state;
        const planned = (preview?.route.path || pieces).includes(node) && !current;
        const added = node === pieces.at(-1);
        const available = !selected.includes(node) && domain.edges.some(edge => edge.source === endpoint && edge.target === node);
        entry.button.classList.toggle("current", current);
        entry.button.classList.toggle("planned", planned);
        entry.button.classList.toggle("available", available);
        const hint = current ? "Here" : added ? "Remove" : planned ? "Preview" : available ? "+ Place" : "";
        entry.hint.textContent = hint;
        entry.button.setAttribute("aria-label", [domain.nodes[node], hint].filter(Boolean).join(", "));
        entry.button.setAttribute("aria-disabled", String(!available && !added));
      }
      const programPath = (preview?.kind === "travel" ? preview.route.path : null) || (pieces.length ? selected :
        session.events.findLast(event => event.kind === "travel")?.path || selected);
      const placedPath = preview?.kind === "travel" ? programPath : selected;
      byId("pieces").replaceChildren(...placedPath.map((node, index) =>
        make("li", (index === 0 ? "● " : node === "G" ? "⚑ " : "→ ") + domain.nodes[node])));
      set("program", JSON.stringify(compile(programPath), null, 2));
      set("program-note", preview?.kind === "reverse" ?
        "Reverse restores a checkpoint. This program shows the last forward route." :
        "Each placed move compiles into one directed transition. The final piece is the declared target.");
      set("trace-summary", session.events.length ? session.events.length + " recorded actions. Reversal preserves earlier events." :
        "No actions have been committed.");
      byId("trace").replaceChildren(...session.events.map(event => make("li", event.kind === "travel" ?
        "Travel · " + labels(event.path) : event.kind === "reverse" ? "Reverse · positional checkpoint" :
          "Revision · " + event.now + " s · check " + (event.check ? "available" : "unavailable"))));
      byId("reverse").disabled = !session.checkpoints.length && preview?.kind !== "travel";
      byId("travel").disabled = session.state === "G";
      byId("clear").disabled = !pieces.length && !preview;
      byId("guide").disabled = session.state === "G";
      byId("preview-card").hidden = preview === null;
      if (preview) {
        set("preview-path", labels(preview.route.path));
        set("preview-basis", preview.kind === "travel" ?
          "Arrives at " + preview.route.arrival + " s · cost " + preview.route.cost +
          " · current context only. Any revision invalidates this preview." :
          "Restore the previous position and preserve the current clock and recorded history.");
        set("confirm", preview.kind === "travel" ? "Move now" : "Restore position");
      }
      byId("certificate").hidden = certificate === null;
      if (certificate) byId("certificate").replaceChildren(make("h3", "Scope of the check"), make("p", certificate));
      opportunity(routeRows);
      byId("new-clock").min = String(session.now);
    }

    function refreshInputs() {
      byId("new-clock").value = String(session.now);
      byId("new-check").checked = session.check;
    }

    function commit(event, message) {
      session = transition(domain, session, event);
      if (event.kind !== "revision") pieces = [];
      preview = null;
      certificate = null;
      proposedRevision = null;
      byId("apply-revision").disabled = true;
      refreshInputs();
      render();
      say(message);
    }

    function showView(view) {
      for (const tab of views) {
        const active = tab.dataset.view === view;
        byId(tab.dataset.view).hidden = !active;
        if (active) tab.setAttribute("aria-current", "page");
        else tab.removeAttribute("aria-current");
      }
      const heading = byId(view + "-title");
      heading.tabIndex = -1;
      heading.focus();
    }

    for (const node of keys) {
      const entry = make("button");
      entry.id = "tile-" + node;
      entry.className = "tile tile-" + node;
      const symbol = make("span", tileSymbols[node] || "◇");
      symbol.className = "symbol";
      symbol.setAttribute("aria-hidden", "true");
      const hint = make("span", "");
      hint.className = "hint";
      hint.setAttribute("aria-hidden", "true");
      entry.append(symbol, make("strong", domain.nodes[node]), hint);
      entry.addEventListener("click", run(() => selectMove(node)));
      byId("board").append(entry);
      tile[node] = {button: entry, hint};
    }
    for (const tab of views) tab.addEventListener("click", () => showView(tab.dataset.view));

    button("guide", () => {
      const path = bestRoutes().find(route => route.eligible)?.path;
      requireThat(path?.length > 1, "No guided route fits this window. Inspect Advanced.");
      pieces = [];
      selectMove(path[1]);
      tile[path[1]].button.focus();
    });
    button("clear", () => { pieces = []; preview = null; render(); say("Pieces cleared."); });
    button("pinpoint", () => {
      say("You are at " + domain.nodes[session.state] + ". " + session.now + " seconds on the simulated clock.");
      tile[session.state].button.focus();
    });
    button("travel", () => {
      const route = pieces.length ? moveRoute(domain, session, selectedPath()) : bestRoutes().find(row => row.eligible);
      requireThat(route?.path.length > 1, "No eligible move reaches the goal. Inspect Advanced for the timing and check.");
      preview = {kind: "travel", route, revision: session.revision};
      render();
      say("Preview ready. " + labels(route.path) + ".");
      byId("confirm").focus();
    });
    button("reverse", () => {
      if (preview?.kind === "travel") {
        preview = null;
        render();
        say("Move preview cleared.");
        return;
      }
      const earlier = session.checkpoints.at(-1);
      requireThat(earlier, "Travel first to create a position checkpoint.");
      preview = {kind: "reverse", route: {path: [session.state, earlier]}, revision: session.revision};
      render();
      say("Reverse preview ready.");
      byId("confirm").focus();
    });
    button("confirm", () => {
      requireThat(preview?.revision === session.revision, "The context changed. Preview the action again.");
      if (preview.kind === "travel") {
        const target = preview.route.path.at(-1);
        commit({kind: "travel", path: preview.route.path}, target === "G" ? "Goal reached." : "Moved to " + domain.nodes[target] + ".");
      } else {
        commit({kind: "reverse"}, "Earlier position restored. Clock and history kept.");
      }
      byId("pinpoint").focus();
    });
    button("check", () => {
      const row = bestRoutes().find(route => route.eligible);
      const lastTravel = session.events.findLast(event => event.kind === "travel");
      if (session.state === "G" && lastTravel?.kind === "travel") {
        certificate = "Verified that the simulated position reached the declared goal along " + labels(lastTravel.path) +
          ". The result records practice state and route feasibility.";
      } else if (row) {
        certificate = "Verified minimum cost " + row.cost + " among eligible paths in this finite model. " +
          labels(row.path) + " reaches the goal by second " + row.arrival + ".";
      } else {
        certificate = "Every path in this finite practice model missed the current deadline or prerequisite.";
      }
      render();
      say(certificate);
    });

    byId("revision-form").addEventListener("submit", run(event => {
      event.preventDefault();
      const now = byId("new-clock").valueAsNumber;
      const check = byId("new-check").checked;
      validateContext(now, check, session.now);
      requireThat(now !== session.now || check !== session.check, "Change the clock or check to preview a revision.");
      proposedRevision = {now, check, revision: session.revision};
      const next = routes(domain, session.state, "G", now, check).find(route => route.eligible);
      set("revision-diff", "Clock " + session.now + " → " + now + " s. Check " +
        (session.check ? "available" : "unavailable") + " → " + (check ? "available" : "unavailable") +
        ". " + (next ? "Next eligible route " + labels(next.path) + "." : "No eligible route remains.") +
        " Applying clears outstanding action previews.");
      byId("apply-revision").disabled = false;
      say("Revision preview ready.");
    }));
    for (const id of ["new-clock", "new-check"]) byId(id).addEventListener("input", () => {
      proposedRevision = null;
      byId("apply-revision").disabled = true;
      set("revision-diff", "");
    });
    button("apply-revision", () => {
      requireThat(proposedRevision?.revision === session.revision, "The state changed. Preview the revision again.");
      commit({kind: "revision", now: proposedRevision.now, check: proposedRevision.check},
        "Revision applied. Earlier action previews have expired.");
      set("revision-diff", "Revision applied and recorded in the trace.");
    });

    function renderTemporal() {
      const view = data.temporal.views[byId("temporal-select").value];
      const panel = byId("temporal-output");
      panel.replaceChildren(make("h3", view.label), make("p", view.summary));
      for (const claim of view.claims) {
        panel.append(make("h3", claim.title));
        const state = claim.value.status || "UNKNOWN";
        const value = Object.hasOwn(claim.value, "value") ? JSON.stringify(claim.value.value) : "";
        panel.append(make("p", claim.kind + " · " + state + (value ? " · " + value : "")));
        if (claim.source) {
          const source = make("p");
          safeLink(source, claim.source);
          panel.append(source);
        }
        if (claim.next_check) panel.append(make("p", claim.next_check));
        details(panel, "Timing and scope", {times: claim.times, value: claim.value});
      }
      if (view.conflicts?.length) details(panel, "Unresolved alternatives", view.conflicts);
    }

    function renderCase() {
      const entry = data.evidence.cases[byId("case-select").value][byId("case-operation").value];
      const panel = byId("case-output");
      panel.replaceChildren(make("h3", entry.summary), make("p", entry.status));
      if (entry.next_check) panel.append(make("p", entry.next_check));
      if (entry.limits?.length) {
        const limits = make("ul");
        limits.append(...entry.limits.map(limit => make("li", limit)));
        panel.append(limits);
      }
      details(panel, "Evidence, model, and calculations", entry.details);
      for (const source of entry.sources || []) {
        const line = make("p");
        safeLink(line, source);
        panel.append(line);
      }
    }

    function renderDecision() {
      const model = data.decisions;
      const result = model.result;
      const names = {"act-now": "Act now", "wait-for-check": "Wait for the check",
        "prepare-reversibly": "Prepare reversibly", fallback: "Use the fallback", "let-close": "Let it close"};
      const list = ids => ids.map(id => names[id] || id).join(", ");
      const number = ({numerator, denominator}) => numerator / denominator;
      const ratioText = ratio => ratio.denominator === 1 ? String(ratio.numerator) : String(number(ratio));
      const percent = ratio => new Intl.NumberFormat("en", {style: "percent", maximumFractionDigits: 2})
        .format(number(ratio));
      set("model-choice", list(result.expected_value_choice) + " leads on expected value. " +
        list(result.robust_choice) + " leads on worst-case value.");
      set("model-boundary", "Separate synthetic example. The model gives " + result.deadline +
        " seconds" + (result.equality ? ", including the deadline." : ", excluding the deadline.") +
        " Scenario weights and utilities are illustrative.");
      const threshold = model.act_now_vs_fallback.probability;
      set("model-threshold", "Acting now beats the fallback above " + percent(threshold) +
        " success probability and ties it at " + percent(threshold) + " under the modeled utilities.");
      byId("model-actions").replaceChildren(...result.actions.map(action => {
        const row = make("tr");
        const heading = make("th", names[action.id] || action.id);
        heading.scope = "row";
        row.append(heading);
        for (const value of action.eligible ? [ratioText(action.expected_value), String(action.worst_value),
          percent(action.p_success_and_on_time)] : ["Ineligible", "—", "—"]) {
          row.append(make("td", value));
        }
        return row;
      }));
      byId("model-limits").replaceChildren(...model.limits.map(limit => make("li", limit)));
      set("model-inputs", JSON.stringify(model.inputs, null, 2));
    }

    function download(name, mime, value) {
      const link = make("a");
      const url = URL.createObjectURL(new Blob([value], {type: mime}));
      link.href = url;
      link.download = name;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    button("export", () => {
      download("future-reader-checkpoint.json", "application/json", JSON.stringify(checkpoint(domain, session), null, 2) + "\n");
      say("Practice checkpoint downloaded.");
    });
    button("export-record", () => {
      const portable = {schema: RECORD, checkpoint: checkpoint(domain, session),
        temporal: data.temporal, evidence: data.evidence, decisions: data.decisions,
        prompt: data.prompt, prompt_sha256: data.prompt_sha256, supplements: data.supplements};
      download("future-reader-record.json", "application/json", JSON.stringify(portable, null, 2) + "\n");
      say("Portable record downloaded.");
    });
    button("download-decision", () => {
      download("future-reader-decision-example.json", "application/json", JSON.stringify(data.decisions, null, 2) + "\n");
      say("Synthetic decision example downloaded.");
    });
    byId("import").addEventListener("change", async () => {
      const file = byId("import").files[0];
      imported = null;
      byId("restore-import").hidden = true;
      if (!file) return;
      try {
        requireThat(file.size <= MAX_IMPORT_BYTES, "Choose a checkpoint no larger than 256 KB.");
        const raw = await file.text();
        if (byId("import").files[0] !== file) return;
        const restored = restore(domain, JSON.parse(raw));
        imported = {restored, revision: session.revision};
        set("import-status", "Practice verified by replay · " + domain.nodes[restored.state] + " · " +
          restored.now + " s · " + restored.events.length + " actions.");
        byId("restore-import").hidden = false;
        say("Checkpoint ready to restore.");
      } catch (error) {
        if (byId("import").files[0] === file) set("import-status", "Checkpoint rejected. " + error.message);
      }
    });
    button("restore-import", () => {
      requireThat(imported && imported.revision === session.revision, "The session changed. Choose the checkpoint again.");
      alternate = session;
      session = imported.restored;
      byId("return-branch").hidden = false;
      resetAfterBranch();
      set("import-status", "Checkpoint restored. The previous practice branch is available.");
      say("Checkpoint restored.");
    });
    button("return-branch", () => {
      [session, alternate] = [alternate, session];
      resetAfterBranch();
      set("import-status", "Previous practice branch restored.");
      say("Previous practice branch restored.");
    });
    function resetAfterBranch() {
      imported = null;
      preview = null;
      proposedRevision = null;
      pieces = [];
      certificate = null;
      byId("restore-import").hidden = true;
      byId("apply-revision").disabled = true;
      byId("import").value = "";
      set("revision-diff", "");
      refreshInputs();
      render();
    }
    const selectedBrief = () => briefs[byId("prompt-version").value];
    function renderBrief() {
      const brief = selectedBrief();
      set("prompt", brief.text);
      set("prompt-hash", "Source SHA-256 " + brief.sha256);
    }
    selectValues("prompt-version", Object.entries(briefs).map(([key, brief]) => [key, brief.title]));
    byId("prompt-version").addEventListener("change", renderBrief);
    button("download-prompt", () => {
      const brief = selectedBrief();
      download(brief.filename, "text/plain;charset=utf-8", brief.text);
      say("Selected brief downloaded.");
    });
    byId("copy-prompt").addEventListener("click", async () => {
      try {
        await window.navigator.clipboard.writeText(selectedBrief().text);
        say("Selected brief copied.");
      } catch {
        const pre = byId("prompt");
        pre.parentElement.open = true;
        const range = document.createRange();
        range.selectNodeContents(pre);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        pre.focus();
        say("Brief selected. Use your browser’s copy shortcut.");
      }
    });

    const temporal = Object.entries(data.temporal.views).map(([key, view]) => [key, view.label]);
    selectValues("temporal-select", temporal);
    byId("temporal-select").addEventListener("change", renderTemporal);
    const caseLabels = {cern: "CERN timing", cenomanian: "Early Cenomanian", archive: "Archive loss",
      "supplied-studies": "Unreproduced studies"};
    const operations = {PINPOINT: "Pinpoint", TRAVEL: "Travel", REVERSE: "Reverse", CERTIFY: "Check"};
    selectValues("case-select", Object.keys(data.evidence.cases).map(key => [key, caseLabels[key] || key]));
    function selectCase() {
      const available = data.evidence.cases[byId("case-select").value];
      selectValues("case-operation", Object.keys(available).map(key => [key, operations[key] || key]));
      renderCase();
    }
    byId("case-select").addEventListener("change", selectCase);
    byId("case-operation").addEventListener("change", renderCase);
    renderBrief();
    renderDecision();
    const release = data["release-preview"];
    set("release-summary", release.current + " → " + release.candidate + " · " + release.trust + ".");
    byId("release-steps").replaceChildren(...[release.compatibility, release.migration, release.rollback]
      .map(value => make("li", value)));
    refreshInputs();
    render();
    renderTemporal();
    selectCase();
  }

  return {routes, newSession, transition, checkpoint, restore, compile, mount};
})();

if (typeof module === "object" && module.exports) module.exports = Companion;
if (typeof document === "object") Companion.mount(document, JSON.parse(document.getElementById("companion-data").textContent));
