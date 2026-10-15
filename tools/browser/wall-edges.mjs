// Edge cases the happy path does not reach: failures that arrive asynchronously, and a pane whose agent is gone.
//
// A global `error` trap catches synchronous throws. Most UI work fails the other way -- a rejected promise from
// a fetch -- and that arrives on `unhandledrejection`. Both are wired, and both are checked here, because a
// diagnostic that only covers one of the two failure modes is a diagnostic that goes quiet exactly when the
// network does.

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
  if (evidence) console.log(`         ${String(evidence).split("\n").slice(0, 2).join("\n         ")}`);
}

async function main() {
  const page = (await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()).find((t) => t.type === "page");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (!m.id || !pending.has(m.id)) return;
    const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
    m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
  };
  await send("Runtime.enable");
  const ev = async (expression) => {
    const out = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (out.exceptionDetails) throw new Error(out.exceptionDetails.exception?.description || out.exceptionDetails.text);
    return out.result.value;
  };
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  console.log("=".repeat(78));
  console.log("  THE WALL, EDGE CASES");
  console.log("=".repeat(78));
  console.log();

  await send("Page.navigate", { url: BASE });
  await wait(4500);
  await ev(`document.getElementById("wallbtn").click()`);
  await wait(2200);

  // ---- a rejected promise must surface ------------------------------------------------------
  //
  // A REAL REJECTION FROM THE PAGE, and getting this to be deterministic took two attempts worth recording.
  //
  // Attempt 1 wrote `Promise.reject(...)` directly in the evaluate expression and checked the error box. It
  // reported zero rows, which looked like a broken trap. It was not: a rejection created inside a
  // `Runtime.evaluate` runs in that evaluation's own context with its own microtask checkpoint, so the page's
  // listener may never see it. The check was measuring the harness, not the product.
  //
  // Attempt 2 fixed that by having the page create the rejection -- and it was still flaky, because it zeroed
  // the counter and then created the rejection in a SEPARATE evaluate, so the listener could be added after
  // the rejection had already been reported.
  //
  // This version arms and fires in ONE expression, so there is no window between them, and it asserts the
  // counter as a PRECONDITION: if the page did not produce a rejection, the check says so rather than
  // silently passing or blaming the trap.
  const armed = await ev(`(() => {
    window.__rejSeen = 0;
    window.__rejBoxRowsBefore = document.querySelectorAll("#wall-errors .wall-error-row").length;
    window.addEventListener("unhandledrejection", () => { window.__rejSeen++; }, { once: true });
    // Created by the page, from a callback the page owns, so the browser really does report it as unhandled.
    setTimeout(() => { Promise.reject(new Error("network went away")); }, 0);
    return true;
  })()`);
  // POLLED, not slept. A fixed wait made this check flaky -- it passed against one server and failed against
  // another with identical code, because how long the browser takes to report an unhandled rejection depends
  // on what else the page is doing. Waiting for the STATE instead of for a duration removes the race, and a
  // check that fails intermittently is worse than no check: it teaches people to re-run until it is green.
  let afterRejection = null;
  for (let i = 0; i < 20; i++) {
    await wait(400);
    afterRejection = await ev(`({
      pageSaw: window.__rejSeen,
      rowsBefore: window.__rejBoxRowsBefore,
      hidden: document.getElementById("wall-errors").hidden,
      rows: document.querySelectorAll("#wall-errors .wall-error-row").length,
      text: [...document.querySelectorAll("#wall-errors .wall-error-row")].map(n => n.textContent).join(" | "),
    })`);
    if (afterRejection.rows > afterRejection.rowsBefore) break;
  }
  check("the page really did produce a rejection (this check's own precondition)",
    afterRejection.pageSaw > 0, `pageSaw=${afterRejection.pageSaw}`);
  check("an unhandled promise rejection becomes visible too, not just a sync throw",
    afterRejection.hidden === false && afterRejection.rows > afterRejection.rowsBefore,
    `${afterRejection.rowsBefore} -> ${afterRejection.rows} rows: ${afterRejection.text.slice(0, 100)}`);
  check("and the report names it as a rejection rather than a generic error",
    /rejection/i.test(afterRejection.text), `"${afterRejection.text.slice(0, 90)}"`);

  // ---- a pane whose locus no longer exists --------------------------------------------------
  await ev(`window.myriaxonWall.setPanes(["ghost-agent-that-does-not-exist", "", "", ""])`);
  await wait(1500);
  const ghost = await ev(`(() => {
    const pane = [...document.querySelectorAll("#wallgrid .pane")]
      .find(p => (p.querySelector(".pane-name")||{}).textContent === "ghost-agent-that-does-not-exist");
    if (!pane) return { rendered: false };
    return {
      rendered: true,
      status: (pane.querySelector(".pane-what")||{}).textContent || "",
      dot: (pane.querySelector(".pane-dot")||{}).className || "",
      turns: pane.querySelectorAll(".pane-turn").length,
    };
  })()`);
  check("a pane for a missing agent still renders rather than vanishing",
    ghost.rendered === true, `status="${ghost.status}"`);
  check("and it says the agent is not there", /not found/i.test(ghost.status), `"${ghost.status}"`);

  // ---- an empty grid of nine must not break --------------------------------------------------
  await ev(`document.querySelector('[data-wall-cols="3"]').click()`);
  await wait(700);
  await ev(`window.myriaxonWall.setPanes(["","","","","","","","",""])`);
  await wait(900);
  const emptyNine = await ev(`({
    panes: document.querySelectorAll("#wallgrid .pane").length,
    empty: document.querySelectorAll("#wallgrid .pane.empty").length,
    cols: document.getElementById("wallgrid").dataset.cols,
  })`);
  check("an empty 3x3 grid renders all nine slots", emptyNine.panes === 9 && emptyNine.empty === 9,
    `${emptyNine.panes} panes, ${emptyNine.empty} empty`);

  // ---- nine real panes must not multiply the requests -----------------------------------------
  await ev(`window.myriaxonWall.setPanes(["anker","cowork","sales","principal-se","anker","cowork","sales","principal-se","anker"])`);
  await wait(1200);
  const requests = await ev(`(async () => {
    let calls = 0;
    const real = window.fetch;
    window.fetch = function (...a) { if (String(a[0]).includes("/api/loci")) calls++; return real.apply(this, a); };
    await window.myriaxonWall.refresh();
    await window.myriaxonWall.refresh();
    window.fetch = real;
    return { calls, panes: document.querySelectorAll("#wallgrid .pane:not(.empty)").length };
  })()`);
  check("nine panes still cost ONE request per refresh, not nine",
    requests.calls === 2 && requests.panes === 9,
    `${requests.calls} calls for ${requests.panes} filled panes (2 refreshes)`);

  // ---- a duplicated locus in two panes --------------------------------------------------------
  const dupes = await ev(`({
    filled: document.querySelectorAll("#wallgrid .pane:not(.empty)").length,
    names: [...document.querySelectorAll("#wallgrid .pane-name")].map(n => n.textContent),
  })`);
  check("the same agent can be watched in more than one pane", dupes.filled === 9,
    `${dupes.filled} panes: ${JSON.stringify(dupes.names.slice(0, 4))}`);

  // ---- the error box does not grow without limit ----------------------------------------------
  const bounded = await ev(`(async () => {
    for (let i = 0; i < 12; i++) {
      window.dispatchEvent(new ErrorEvent("error", { message: "distinct failure " + i, error: new Error("distinct failure " + i) }));
    }
    await new Promise(r => setTimeout(r, 400));
    return { rows: document.querySelectorAll("#wall-errors .wall-error-row").length,
             head: (document.querySelector(".wall-error-head")||{}).textContent || "" };
  })()`);
  check("the failure report stays bounded instead of growing forever",
    bounded.rows <= 4, `${bounded.rows} rows shown; "${bounded.head}"`);

  const passed = results.filter((r) => r.ok).length;
  const failed = results.filter((r) => !r.ok);
  console.log();
  console.log("=".repeat(78));
  console.log(`  ${passed}/${results.length} checks passed`);
  failed.forEach((f) => console.log(`    - FAILED: ${f.name}`));
  console.log("=".repeat(78));
  ws.close();
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.log("  DRIVER ERROR: " + e.message); process.exit(2); });
