// The hard half of the wall: the claims the first driver could not actually prove.
//
// Three of its checks passed for the wrong reason and would have shipped a feature that looks validated:
//
//   1. "dragging a pane moves it" compared the filled names BEFORE and AFTER and found them equal. With no
//      empty slot the move is a no-op, so the assertion could never fail. It proved nothing.
//   2. "the API has a real conversation" checked the API, not the pane. It is not evidence the pane draws.
//   3. Nothing exercised the cross-agent lines at all -- the one thing that makes this a communication plane
//      rather than nine chat boxes -- and nothing proved a failure becomes VISIBLE rather than silent.
//
// So this driver puts an empty slot in the grid and moves a named pane into a named slot, injects a real
// AGENTS-layer event and checks the join renders, forces a failure and checks the page says so, and then
// sends an actual message from a pane and waits for the agent to answer in it.

const CDP_PORT = Number(process.env.MYX_CDP_PORT || 9222);
const BASE = process.env.MYX_BASE || "http://127.0.0.1:8765";
let nextId = 1;
const pending = new Map();
let ws;

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error(method + " timeout")); } }, 40000);
  });
}

const results = [];
function check(name, ok, evidence = "") {
  results.push({ name, ok });
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}`);
  if (evidence) console.log(`         ${String(evidence).split("\n").slice(0, 3).join("\n         ")}`);
}

async function main() {
  const page = (await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()).find((t) => t.type === "page");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (!m.id || !pending.has(m.id)) return;
    const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
    m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
  };
  await send("Runtime.enable");

  async function ev(expression) {
    const out = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (out.exceptionDetails) throw new Error(out.exceptionDetails.exception?.description || out.exceptionDetails.text);
    return out.result.value;
  }
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  console.log("=".repeat(78));
  console.log("  THE WALL, THE HARD HALF");
  console.log("=".repeat(78));
  console.log();

  await send("Page.navigate", { url: BASE });
  await wait(4500);
  await ev(`document.getElementById("wallbtn").click()`);
  await wait(2500);

  // ---- 1. A REAL DRAG, with an empty slot so the move is observable --------------------------
  await ev(`document.querySelector('[data-wall-cols="3"]').click()`);   // 9 panes, so empties exist
  await wait(900);
  await ev(`window.myriaxonWall.setPanes(["anker", "", "", "", "", "", "", "", ""])`);
  await wait(900);

  const before = await ev(`[...document.querySelectorAll("#wallgrid .pane")]
    .map((p) => (p.querySelector(".pane-name")||{}).textContent || "")`);
  check("the grid has a named pane and empty targets to move it to",
    before[0] === "anker" && before.filter(Boolean).length === 1,
    `before=${JSON.stringify(before.slice(0, 4))}`);

  const moved = await ev(`(() => {
    const panes = [...document.querySelectorAll("#wallgrid .pane")];
    const src = panes[0].querySelector(".pane-bar");
    const dst = panes[5];
    const dt = new DataTransfer();
    src.dispatchEvent(new DragEvent("dragstart", { bubbles: true, dataTransfer: dt }));
    dst.dispatchEvent(new DragEvent("dragover", { bubbles: true, dataTransfer: dt }));
    dst.dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer: dt }));
    return true;
  })()`);
  await wait(1200);
  const after = await ev(`[...document.querySelectorAll("#wallgrid .pane")]
    .map((p) => (p.querySelector(".pane-name")||{}).textContent || "")`);
  check("dragging a pane MOVES it: the name leaves slot 0 and appears in slot 5",
    before[0] === "anker" && after[5] === "anker" && after[0] === "",
    `after=${JSON.stringify(after.slice(0, 7))}`);

  const persisted = await ev(`(JSON.parse(localStorage.getItem("myriaxon.wall")||"{}").panes||[])[5]`);
  check("and the move is persisted, so a reload keeps the arrangement", persisted === "anker",
    `stored slot 5 = ${JSON.stringify(persisted)}`);

  // ---- 2. TWO panes swapped, verified positionally -------------------------------------------
  await ev(`window.myriaxonWall.setPanes(["anker", "cowork", "", "", "", "", "", "", ""])`);
  await wait(900);
  await ev(`(() => {
    const panes = [...document.querySelectorAll("#wallgrid .pane")];
    const dt = new DataTransfer();
    dt.setData("text/plain", "pane:0");
    panes[1].dispatchEvent(new DragEvent("dragover", { bubbles: true, dataTransfer: dt }));
    panes[1].dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer: dt }));
  })()`);
  await wait(1000);
  const swapped = await ev(`[...document.querySelectorAll("#wallgrid .pane")]
    .map((p) => (p.querySelector(".pane-name")||{}).textContent || "").slice(0, 3)`);
  check("dropping pane 0 onto pane 1 SWAPS them",
    swapped[0] === "cowork" && swapped[1] === "anker",
    `slots 0,1 = ${JSON.stringify(swapped.slice(0, 2))} (was anker, cowork)`);

  // ---- 3. THE CROSS-AGENT JOIN: a real AGENTS event must appear in the panes ------------------
  await ev(`window.myriaxonWall.setPanes(["anker", "cowork", "", "", "", "", "", "", ""])`);
  await wait(700);
  await ev(`document.getElementById("wall-links").checked = true;
            document.getElementById("wall-links").dispatchEvent(new Event("change"))`);
  await wait(600);

  const injected = await ev(`(() => {
    // Exactly the shape the transparency stream sends for an AGENTS-layer event, pushed through the page's
    // OWN handler so the filter under test is the filter that ships.
    const event = {
      type: "event", layer: 6, kind: "agent", at: Date.now() / 1000,
      locus: "anker", summary: "anker: posted to the board task-42",
      detail: { member: "anker", what: "posted to the board task-42" },
      trusted: false,
    };
    const before = document.querySelectorAll("#wallgrid .pane-link").length;
    const recognized = window.myriaxonWall.isAgentEvent(event);
    window.myriaxonWall.pushEvent(event);
    const after = document.querySelectorAll("#wallgrid .pane-link").length;
    const text = [...document.querySelectorAll("#wallgrid .pane-link")].map((n) => n.textContent);
    return { before, after, text, recognized };
  })()`);
  check("the stream filter recognises an AGENTS-layer event", injected.recognized === true);
  check("an AGENTS-layer event renders as a cross-agent line in the pane",
    injected.after > injected.before && injected.text.some((t) => t.includes("board")),
    `${injected.before} -> ${injected.after} links; ${JSON.stringify(injected.text.slice(0, 2))}`);

  const attributed = await ev(`(() => {
    const panes = [...document.querySelectorAll("#wallgrid .pane")];
    const anker = panes.find((p) => (p.querySelector(".pane-name")||{}).textContent === "anker");
    const cowork = panes.find((p) => (p.querySelector(".pane-name")||{}).textContent === "cowork");
    return {
      ankerLinks: anker ? anker.querySelectorAll(".pane-link").length : -1,
      coworkLinks: cowork ? cowork.querySelectorAll(".pane-link").length : -1,
    };
  })()`);
  check("and it is attributed to the RIGHT agent, not shown in every pane",
    attributed.ankerLinks === 1 && attributed.coworkLinks === 0,
    `anker=${attributed.ankerLinks} cowork=${attributed.coworkLinks}`);

  const offByToggle = await ev(`(() => {
    document.getElementById("wall-links").checked = false;
    document.getElementById("wall-links").dispatchEvent(new Event("change"));
    return document.querySelectorAll("#wallgrid .pane-link").length;
  })()`);
  check("turning the join off removes the lines", offByToggle === 0, `${offByToggle} links remain`);

  // ---- 4. FAILURES MUST BE VISIBLE, not silent -------------------------------------------------
  const failure = await ev(`(() => {
    const before = { hidden: document.getElementById("wall-errors").hidden,
                     rows: document.querySelectorAll("#wall-errors .wall-error-row").length };
    window.myriaxonWall.state.__probe = true;
    // Throw from inside the render path, exactly as the real bug did.
    try { (function bad(){ return undefined.notAFunction; })(); } catch (err) {
      window.dispatchEvent(new ErrorEvent("error", { message: err.message, error: err }));
    }
    const after = { hidden: document.getElementById("wall-errors").hidden,
                    rows: document.querySelectorAll("#wall-errors .wall-error-row").length,
                    head: (document.querySelector(".wall-error-head")||{}).textContent || "",
                    text: (document.querySelector(".wall-error-row")||{}).textContent || "" };
    return { before, after };
  })()`);
  check("a thrown error becomes VISIBLE on the page, not only in the console",
    failure.after.hidden === false && failure.after.rows > 0,
    `hidden=${failure.after.hidden} rows=${failure.after.rows}`);
  check("and the message says this is a bug rather than an empty room",
    /bug/i.test(failure.after.head), `"${failure.after.head}"`);
  check("and it names the actual error", /notAFunction|not a function/i.test(failure.after.text),
    `"${failure.after.text.slice(0, 80)}"`);

  const cleared = await ev(`(async () => {
    await window.myriaxonWall.refresh();
    return { hidden: document.getElementById("wall-errors").hidden,
             rows: document.querySelectorAll("#wall-errors .wall-error-row").length };
  })()`);
  check("a successful refresh clears the report, so it does not become wallpaper",
    cleared.hidden === true && cleared.rows === 0,
    `hidden=${cleared.hidden} rows=${cleared.rows}`);

  // ---- 5. THE CONTROL PLANE: send a message from a pane, get an answer in it --------------------
  await ev(`window.myriaxonWall.setPanes(["principal-se", "", "", "", "", "", "", "", ""])`);
  await wait(800);
  const sent = await ev(`(() => {
    const pane = [...document.querySelectorAll("#wallgrid .pane")]
      .find((p) => (p.querySelector(".pane-name")||{}).textContent === "principal-se");
    if (!pane) return { ok: false, why: "no pane" };
    const box = pane.querySelector(".pane-composer textarea");
    const form = pane.querySelector(".pane-composer");
    if (!box || !form) return { ok: false, why: "no composer" };
    box.value = "Reply with exactly one word: WALLTEST";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    return { ok: true, status: document.getElementById("wallstatus").textContent };
  })()`);
  check("a pane can send a message to its agent", sent.ok === true, sent.why || `status="${sent.status}"`);

  // Wait for the agent to answer, polling the pane itself.
  //
  // The FIRST version of this check accepted any turn whose text contained WALLTEST -- and the user's own
  // message contains it, so it passed the instant the message was echoed back, before the agent had said
  // anything. A check that its own precondition satisfies proves nothing. This one requires a turn labelled
  // with the LOCUS's name, which is the only thing the agent's reply can be.
  let answered = null;
  for (let i = 0; i < 50; i++) {
    await wait(3000);
    answered = await ev(`(() => {
      const pane = [...document.querySelectorAll("#wallgrid .pane")]
        .find((p) => (p.querySelector(".pane-name")||{}).textContent === "principal-se");
      if (!pane) return null;
      const turns = [...pane.querySelectorAll(".pane-turn")];
      const fromAgent = turns.filter((t) => {
        const who = (t.querySelector(".pane-who")||{}).textContent || "";
        return who.trim().toLowerCase() === "principal-se";
      });
      const lastAgent = fromAgent.length
        ? fromAgent[fromAgent.length - 1].querySelector(".pane-bubble").textContent
        : "";
      return {
        total: turns.length,
        fromAgent: fromAgent.length,
        lastAgent: lastAgent.slice(0, 160),
        status: document.getElementById("wallstatus").textContent,
      };
    })()`);
    if (answered && answered.fromAgent > 0 && answered.lastAgent.trim().length > 0) break;
  }
  check("the agent's own reply arrives IN THE PANE (not just the echoed message)",
    !!answered && answered.fromAgent > 0 && answered.lastAgent.trim().length > 0,
    answered ? `turns=${answered.total} from agent=${answered.fromAgent} reply="${answered.lastAgent.slice(0, 80)}"`
             : "no pane");
  check("and the reply is the agent's, not the message replayed back",
    !!answered && !/^you/i.test(answered.lastAgent.trim()) && !/Message from your owner/i.test(answered.lastAgent),
    answered ? `"${answered.lastAgent.slice(0, 90)}"` : "none");

  const errors = await ev(`window.myriaxonWall.errors()`);
  check("no hidden failures accumulated during a real turn",
    !errors || errors.length === 0,
    errors && errors.length ? JSON.stringify(errors).slice(0, 200) : "clean");

  const passed = results.filter((r) => r.ok).length;
  const failed = results.filter((r) => !r.ok);
  console.log();
  console.log("=".repeat(78));
  console.log(`  ${passed}/${results.length} checks passed`);
  if (failed.length) {
    console.log(`  ${failed.length} FAILED:`);
    failed.forEach((f) => console.log(`    - ${f.name}`));
  }
  console.log("=".repeat(78));
  ws.close();
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.log("  DRIVER ERROR: " + e.message); process.exit(2); });
