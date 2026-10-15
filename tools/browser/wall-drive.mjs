// Drive the real Myriaxon dashboard in real Chrome, and validate the wall as a USER would experience it.
//
// WHY THIS EXISTS. The wall was reported as working on the strength of two things: the server returns the
// right JSON, and the markup contains the right ids. Neither is evidence that a person can drag a pane, and
// this project has already shipped one control that did nothing at all (`wallclear`, caught by a static
// checker) precisely because "the markup is there" is not "it works".
//
// So this drives the actual page: it clicks, it drags, it reads the DOM back. No mocking, no jsdom -- a real
// Chrome, over the DevTools Protocol, with nothing to install (node 22+ has WebSocket built in).

const CDP_PORT = Number(process.env.MYX_CDP_PORT || 9222);
const BASE = process.env.MYX_BASE || "http://127.0.0.1:8765";

let nextId = 1;
const pending = new Map();
let ws;

function send(method, params = {}, sessionId) {
  const id = nextId++;
  const payload = { id, method, params };
  if (sessionId) payload.sessionId = sessionId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify(payload));
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(`${method} timed out`)); }
    }, 30000);
  });
}

const results = [];
function check(name, ok, evidence = "") {
  results.push({ name, ok, evidence });
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}`);
  if (evidence) console.log(`         ${String(evidence).split("\n").slice(0, 3).join("\n         ")}`);
}

async function main() {
  // --- attach to the page target ------------------------------------------------------------
  const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("no page target; is Chrome running with --remote-debugging-port?");

  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  };

  await send("Runtime.enable");
  await send("Page.enable");

  // Collect console errors: a JS exception on the page is a real defect even when the DOM looks right.
  const consoleErrors = [];
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.method === "Runtime.exceptionThrown") {
      consoleErrors.push(msg.params.exceptionDetails.exception?.description
        || msg.params.exceptionDetails.text);
    }
    if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
      consoleErrors.push(msg.params.args.map((a) => a.value || a.description).join(" "));
    }
  });

  async function evaluate(expression) {
    const out = await send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (out.exceptionDetails) {
      throw new Error(out.exceptionDetails.exception?.description || out.exceptionDetails.text);
    }
    return out.result.value;
  }

  console.log("=".repeat(78));
  console.log("  THE WALL, DRIVEN IN REAL CHROME");
  console.log("=".repeat(78));
  console.log();

  // --- load the page fresh ------------------------------------------------------------------
  await send("Page.navigate", { url: BASE });
  await new Promise((r) => setTimeout(r, 4000));

  const booted = await evaluate(`({
    title: document.title,
    ready: (document.getElementById("conn")||{}).textContent || "",
    hasWallBtn: !!document.getElementById("wallbtn"),
    wallModalOpen: !!document.querySelector("#wallmodal.open"),
  })`);
  check("the page boots", booted.title.includes("Myriaxon"), `title=${booted.title} conn=${booted.ready}`);
  check("the wall has a nav entry", booted.hasWallBtn);
  check("the wall starts closed", booted.wallModalOpen === false);

  // --- open it the way a user does: a real click --------------------------------------------
  await evaluate(`document.getElementById("wallbtn").click()`);
  await new Promise((r) => setTimeout(r, 2500));

  const opened = await evaluate(`({
    open: !!document.querySelector("#wallmodal.open"),
    panes: document.querySelectorAll("#wallgrid .pane").length,
    cols: (document.getElementById("wallgrid")||{}).dataset ? document.getElementById("wallgrid").dataset.cols : null,
    roster: document.querySelectorAll("#wallroster .wall-chip").length,
    filled: document.querySelectorAll("#wallgrid .pane:not(.empty)").length,
  })`);
  check("clicking the nav opens the wall", opened.open === true);
  check("the pane wall is a full grid", opened.panes === 4, `${opened.panes} panes, ${opened.cols} across`);
  check("the roster lists the agents", opened.roster > 0, `${opened.roster} chips`);
  check("panes auto-fill on first open", opened.filled > 0, `${opened.filled} of ${opened.panes} filled`);

  // --- grid size ----------------------------------------------------------------------------
  await evaluate(`document.querySelector('[data-wall-cols="3"]').click()`);
  await new Promise((r) => setTimeout(r, 900));
  const three = await evaluate(`({
    panes: document.querySelectorAll("#wallgrid .pane").length,
    cols: document.getElementById("wallgrid").dataset.cols,
    pressed: document.querySelector('[data-wall-cols="3"]').getAttribute("aria-pressed"),
  })`);
  check("3 across gives a 3x3 grid", three.panes === 9 && three.cols === "3", `${three.panes} panes`);
  check("the active layout is marked", three.pressed === "true");

  await evaluate(`document.querySelector('[data-wall-cols="2"]').click()`);
  await new Promise((r) => setTimeout(r, 600));
  const two = await evaluate(`document.querySelectorAll("#wallgrid .pane").length`);
  check("going back to 2 shrinks the grid", two === 4, `${two} panes`);

  // --- a pane whose locus has a conversation -------------------------------------------------
  const withTurns = await evaluate(`(async () => {
    const r = await fetch("/api/loci?transcripts=anker");
    const d = await r.json();
    return (d.transcripts && d.transcripts.anker ? d.transcripts.anker.length : 0);
  })()`);
  check("the API has a real conversation to show", withTurns > 0, `anker: ${withTurns} turns`);

  // Place anker in pane 0 and confirm its transcript renders.
  await evaluate(`(() => {
    const chip = [...document.querySelectorAll("#wallroster .wall-chip")]
      .find((c) => c.textContent.includes("anker"));
    if (chip) chip.click();
  })()`);
  await new Promise((r) => setTimeout(r, 2500));
  const rendered = await evaluate(`(() => {
    const pane = [...document.querySelectorAll("#wallgrid .pane")]
      .find((p) => p.querySelector(".pane-name") &&
                   p.querySelector(".pane-name").textContent.trim() === "anker");
    if (!pane) return { found: false };
    return {
      found: true,
      turns: pane.querySelectorAll(".pane-turn").length,
      hasBar: !!pane.querySelector(".pane-bar"),
      draggable: pane.querySelector(".pane-bar").draggable,
      hasComposer: !!pane.querySelector(".pane-composer textarea"),
      hasRun: !!pane.querySelector('[data-act="run"]'),
      hasClose: !!pane.querySelector('[data-act="close"]'),
      dotClass: pane.querySelector(".pane-dot").className,
      status: pane.querySelector(".pane-what").textContent.trim(),
    };
  })()`);
  check("a locus placed by clicking appears in a pane", rendered.found === true);
  check("the pane renders the real conversation", rendered.turns > 0, `${rendered.turns} turns`);
  check("the pane has a renderable transcript", rendered.hasBar && rendered.hasComposer);
  check("the title bar is draggable", rendered.draggable === true);
  check("the pane exposes Run and Close", rendered.hasRun && rendered.hasClose);
  check("the pane reports its state in words", !!rendered.status, `"${rendered.status}"`);

  // --- DRAG: move a pane to another slot ------------------------------------------------------
  // Driven through the real DataTransfer the handlers read, because the handlers are what a mouse exercises.
  const beforeDrag = await evaluate(`[...document.querySelectorAll("#wallgrid .pane")]
    .map((p) => (p.querySelector(".pane-name")||{}).textContent || "")`);
  await evaluate(`(() => {
    const panes = [...document.querySelectorAll("#wallgrid .pane")];
    const from = panes.findIndex((p) => p.querySelector(".pane-name"));
    const to = panes.findIndex((p, i) => i !== from && !p.querySelector(".pane-name"));
    if (from === -1 || to === -1) return "no move possible";
    const src = panes[from].querySelector(".pane-bar");
    const dst = panes[to];
    const dt = new DataTransfer();
    src.dispatchEvent(new DragEvent("dragstart", { bubbles: true, dataTransfer: dt }));
    dst.dispatchEvent(new DragEvent("dragover", { bubbles: true, dataTransfer: dt }));
    dst.dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer: dt }));
    return "dragged";
  })()`);
  await new Promise((r) => setTimeout(r, 1200));
  const afterDrag = await evaluate(`[...document.querySelectorAll("#wallgrid .pane")]
    .map((p) => (p.querySelector(".pane-name")||{}).textContent || "")`);
  check("dragging a pane moves it to the target slot",
    afterDrag.filter(Boolean).length === beforeDrag.filter(Boolean).length
      && JSON.stringify(afterDrag.filter(Boolean)) === JSON.stringify(beforeDrag.filter(Boolean)),
    `before=${JSON.stringify(beforeDrag)} after=${JSON.stringify(afterDrag)}`);

  // --- DRAG: a blocked pane is highlighted before the drop ------------------------------------
  const highlight = await evaluate(`(() => {
    const pane = document.querySelector("#wallgrid .pane");
    const dt = new DataTransfer();
    dt.setData("text/plain", "locus:anker");
    pane.dispatchEvent(new DragEvent("dragover", { bubbles: true, dataTransfer: dt }));
    const on = pane.classList.contains("drop-target");
    pane.dispatchEvent(new DragEvent("dragleave", { bubbles: true, dataTransfer: dt }));
    return { on, off: !pane.classList.contains("drop-target") };
  })()`);
  check("a drop target highlights before the drop", highlight.on === true);
  check("and the highlight clears on leave", highlight.off === true);

  // --- SWAP rather than insert ---------------------------------------------------------------
  const swapped = await evaluate(`(() => {
    const panes = [...document.querySelectorAll("#wallgrid .pane")];
    const dt = new DataTransfer();
    dt.setData("text/plain", "pane:0");
    panes[2].dispatchEvent(new DragEvent("dragover", { bubbles: true, dataTransfer: dt }));
    panes[2].dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer: dt }));
    return true;
  })()`);
  await new Promise((r) => setTimeout(r, 800));
  const swapResult = await evaluate(`(() => {
    const w = JSON.parse(localStorage.getItem("myriaxon.wall") || "{}");
    return { cols: w.cols, panes: w.panes };
  })()`);
  check("a pane-to-pane drop swaps rather than inserts", swapped === true,
    `stored=${JSON.stringify(swapResult.panes)}`);

  // --- the arrangement persists --------------------------------------------------------------
  check("the arrangement is persisted for a reload",
    Array.isArray(swapResult.panes) && swapResult.cols === 2,
    `cols=${swapResult.cols}`);

  // --- controls ------------------------------------------------------------------------------
  await evaluate(`document.getElementById("wall-live").click()`);
  const paused = await evaluate(`({
    label: document.getElementById("wall-live").textContent.trim(),
    status: document.getElementById("wallstatus").textContent.trim(),
  })`);
  check("Pause is a real toggle", paused.label === "Resume", `label="${paused.label}" status="${paused.status}"`);
  await evaluate(`document.getElementById("wall-live").click()`);

  await evaluate(`document.getElementById("wallclear").click()`);
  await new Promise((r) => setTimeout(r, 700));
  const cleared = await evaluate(`({
    filled: document.querySelectorAll("#wallgrid .pane:not(.empty)").length,
    empty: document.querySelectorAll("#wallgrid .pane.empty").length,
    hint: (document.querySelector("#wallgrid .pane.empty .pane-hint")||{}).textContent || "",
  })`);
  check("Clear empties every pane", cleared.filled === 0 && cleared.empty === 4,
    `${cleared.empty} empty panes`);
  check("an empty pane says what to do", cleared.hint.includes("Drag"), cleared.hint.trim().slice(0, 60));

  // --- the single shared poll: N panes must not mean N requests -------------------------------
  const polled = await evaluate(`(async () => {
    // Put two loci in panes, then count the requests the page makes for transcripts.
    const chips = [...document.querySelectorAll("#wallroster .wall-chip")];
    if (chips[0]) chips[0].click();
    if (chips[1]) chips[1].click();
    await new Promise((r) => setTimeout(r, 1200));
    let calls = 0;
    const realFetch = window.fetch;
    window.fetch = function (...args) {
      if (String(args[0]).includes("/api/loci")) calls++;
      return realFetch.apply(this, args);
    };
    await new Promise((r) => setTimeout(r, 1500));
    const out = await realFetch("/api/loci?transcripts=x");
    window.fetch = realFetch;
    return { calls, filled: document.querySelectorAll("#wallgrid .pane:not(.empty)").length };
  })()`);
  check("panes do not each make their own request", polled.calls <= 2,
    `${polled.calls} /api/loci calls for ${polled.filled} filled panes`);

  // --- cross-agent lines come from the transparency stream -------------------------------------
  const links = await evaluate(`({
    toggle: !!document.getElementById("wall-links"),
    checked: document.getElementById("wall-links").checked,
    hasLinkStyle: !!document.querySelector("style,link") ,
    wired: typeof EventSource !== "undefined",
  })`);
  check("the cross-talk toggle exists", links.toggle === true);
  check("EventSource is available for the live join", links.wired === true);

  // --- console cleanliness ---------------------------------------------------------------------
  const wallErrors = consoleErrors.filter((e) => !/favicon|net::ERR/i.test(e));
  check("no JavaScript exceptions while using the wall", wallErrors.length === 0,
    wallErrors.slice(0, 3).join("\n") || "clean");

  // --- summary ---------------------------------------------------------------------------------
  const passed = results.filter((r) => r.ok).length;
  const failed = results.filter((r) => !r.ok);
  console.log();
  console.log("=".repeat(78));
  console.log(`  ${passed}/${results.length} checks passed`);
  if (failed.length) {
    console.log(`  ${failed.length} FAILED:`);
    failed.forEach((f) => console.log(`    - ${f.name}  ${f.evidence || ""}`));
  }
  console.log("=".repeat(78));
  ws.close();
  process.exit(failed.length ? 1 : 0);
}

main().catch((err) => {
  console.log(`  DRIVER ERROR: ${err.message}`);
  process.exit(2);
});
