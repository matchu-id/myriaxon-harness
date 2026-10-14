// Render the Myriaxon dashboard in a real browser via the Chrome DevTools Protocol and measure it.
//
//   node scripts/ui_render.js <url> <outDir>
//
// Why CDP and not `--dump-dom` or `--screenshot`: Chrome removed the useful behaviour of those flags in
// headless=new (verified: the flags returned instantly with no output on a trivial page), so the only
// dependable way to render and measure is to drive the browser. Node 24 has a global WebSocket, so this
// needs no packages.
//
// It navigates, waits for the app's own scripts to run, evaluates a measurement expression in the page,
// captures screenshots of the main view and each dialog, and prints the measurements as one JSON line
// prefixed with RESULT: so the Python check can assert on them.

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");

const url = process.argv[2] || "http://127.0.0.1:8765/";
const outDir = process.argv[3] || "bench-out/ui-shots";
const chromePath =
  process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const port = 9333 + (process.pid % 500);

fs.mkdirSync(outDir, { recursive: true });

const profile = path.resolve(outDir, "..", "chrome-cdp-profile");
fs.mkdirSync(profile, { recursive: true });

const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-crash-reporter",
    "--disable-breakpad",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--hide-scrollbars",
    "--window-size=1440,960",
    `--user-data-dir=${profile}`,
    `--remote-debugging-port=${port}`,
    "about:blank",
  ],
  { stdio: ["ignore", "pipe", "pipe"] },
);

let chromeLog = "";
chrome.stderr.on("data", (chunk) => {
  chromeLog += chunk.toString();
});

function getJson(pathname) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port, path: pathname }, (res) => {
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (err) {
            reject(new Error(`bad JSON from ${pathname}: ${body.slice(0, 120)}`));
          }
        });
      })
      .on("error", reject);
  });
}

async function waitForDevTools(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      return await getJson("/json/version");
    } catch {
      await new Promise((r) => setTimeout(r, 250));
    }
  }
  throw new Error("chrome devtools endpoint never came up. log: " + chromeLog.slice(-400));
}

class Session {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(JSON.stringify(message.error)));
        else resolve(message.result);
      }
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`${method} timed out`));
        }
      }, 60000);
    });
  }
}

// The measurement runs inside the page. Anything the browser computes is only knowable here: an
// unresolved CSS variable, a zero-height panel, and a dialog that is visible before being opened are
// all invisible to a check that greps the served HTML.
const PROBE = `(() => {
  const out = { problems: [], vars: {}, controls: {}, dialogs: {} };
  const root = getComputedStyle(document.documentElement);
  for (const name of ["--bg","--panel","--text","--muted","--accent","--border","--code-bg","--add-bg","--del-bg","--ok","--warn","--err","--mono","--hover","--user","--assistant"]) {
    const v = root.getPropertyValue(name).trim();
    out.vars[name] = v;
    if (!v) out.problems.push("unresolved CSS variable " + name);
  }
  const body = getComputedStyle(document.body);
  out.body = { background: body.backgroundColor, color: body.color, font: body.fontFamily };
  const lum = (rgb) => {
    const m = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(rgb || "");
    if (!m) return null;
    return (0.2126 * +m[1] + 0.7152 * +m[2] + 0.0722 * +m[3]) / 255;
  };
  const lb = lum(body.backgroundColor), lt = lum(body.color);
  out.contrast = { background: lb, text: lt };
  if (lb !== null && lt !== null && Math.abs(lb - lt) < 0.25) {
    out.problems.push("low contrast: " + body.color + " on " + body.backgroundColor);
  }
  for (const id of ["codebtn","msgbtn","plugbtn","rulesbtn","mcpbtn","themebtn","settingsbtn","composer","send"]) {
    const el = document.getElementById(id);
    if (!el) { out.problems.push("missing control " + id); continue; }
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    out.controls[id] = { w: Math.round(r.width), h: Math.round(r.height), display: cs.display, visible: cs.visibility };
    if (cs.display === "none") { out.problems.push("control " + id + " is display:none"); continue; }
    if (r.width < 6 || r.height < 6) out.problems.push("control " + id + " has no size (" + Math.round(r.width) + "x" + Math.round(r.height) + ")");
    if (r.right > innerWidth + 2 || r.bottom > innerHeight + 2 || r.left < -2 || r.top < -2) {
      out.problems.push("control " + id + " is off screen at " + JSON.stringify({l:Math.round(r.left),t:Math.round(r.top),r:Math.round(r.right),b:Math.round(r.bottom)}) + " viewport " + innerWidth + "x" + innerHeight);
    }
  }
  const chat = document.getElementById("chat");
  if (chat) {
    const r = chat.getBoundingClientRect();
    out.chat = { h: Math.round(r.height), w: Math.round(r.width), overflowY: getComputedStyle(chat).overflowY };
    if (r.height < 80) out.problems.push("transcript has no height (" + Math.round(r.height) + ")");
  } else out.problems.push("missing #chat");
  for (const id of ["rulesmodal","mcpmodal","plugmodal","msgmodal","codemodal","setmodal"]) {
    const el = document.getElementById(id);
    if (!el) { out.problems.push("missing dialog " + id); continue; }
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    out.dialogs[id] = { display: cs.display, visibility: cs.visibility, h: Math.round(r.height) };
    const shown = cs.display !== "none" && cs.visibility !== "hidden" && r.height > 0;
    if (shown) out.problems.push("dialog " + id + " is visible before being opened");
  }
  out.logo = !!document.querySelector("svg, img");
  out.title = document.title;
  return out;
})()`;

async function main() {
  await waitForDevTools(30000);
  const target = await getJson("/json/list");
  const page = target.find((t) => t.type === "page");
  if (!page) throw new Error("no page target");

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error("websocket failed")), { once: true });
  });
  const session = new Session(ws);
  await session.send("Page.enable");
  await session.send("Runtime.enable");

  const consoleErrors = [];
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Runtime.exceptionThrown") {
      const d = message.params.exceptionDetails;
      consoleErrors.push(d.exception?.description || d.text || "unknown exception");
    }
  });

  const loaded = new Promise((resolve) => {
    const handler = (event) => {
      const message = JSON.parse(event.data);
      if (message.method === "Page.loadEventFired") resolve();
    };
    ws.addEventListener("message", handler);
    setTimeout(resolve, 20000);
  });
  await session.send("Page.navigate", { url });
  await loaded;
  // The app connects its SSE stream and paints the first frame after load, so give it a moment before
  // measuring; measuring too early reports zero-height panels that appear a frame later.
  await new Promise((r) => setTimeout(r, 2500));

  const measured = await session.send("Runtime.evaluate", { expression: PROBE, returnByValue: true });
  const result = measured.result.value || {};

  async function shot(name) {
    const png = await session.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    const file = path.join(outDir, name);
    fs.writeFileSync(file, Buffer.from(png.data, "base64"));
    return { file, bytes: fs.statSync(file).size };
  }

  const shots = {};
  // Dialogs are captured in dark mode. The theme probe below toggles to light, so without pinning it
  // here the dialog screenshots came out light while being labelled as the dark set.
  await session.send("Runtime.evaluate", {
    expression: `(() => {
      const wanted = "dark";
      if ((document.documentElement.dataset.theme || "dark") !== wanted) {
        const b = document.getElementById("themebtn");
        if (b) b.click();
      }
    })()`,
  });
  await new Promise((r) => setTimeout(r, 500));
  shots.main = await shot("dashboard-dark.png");

  // Open each dialog and screenshot it. A dialog that renders as an empty box is a failure this
  // catches and a markup check cannot.
  const dialogIds = ["codemodal", "plugmodal", "rulesmodal", "msgmodal", "setmodal", "mcpmodal"];
  const dialogButtons = {
    codemodal: "codebtn", plugmodal: "plugbtn", rulesmodal: "rulesbtn",
    msgmodal: "msgbtn", setmodal: "settingsbtn", mcpmodal: "mcpbtn",
  };
  for (const id of dialogIds) {
    // The real button is clicked, not just the class added: each panel loads its own data in its
    // click handler, so opening the dialog directly produced an empty box that looked like a bug in
    // the panel.
    const opened = await session.send("Runtime.evaluate", {
      expression: `(() => {
        const button = document.getElementById(${JSON.stringify(dialogButtons[id])});
        if (button) button.click();
        const el = document.getElementById(${JSON.stringify(id)});
        if (!el) return { ok: false, why: "missing" };
        el.classList.add("open");
        const r = el.getBoundingClientRect();
        const dlg = el.querySelector(".dialog");
        const dr = dlg ? dlg.getBoundingClientRect() : null;
        const rows = el.querySelectorAll(".filerow, .vendrow, .recent").length;
        return { ok: true, h: Math.round(r.height), rows,
                 dialog: dr ? { w: Math.round(dr.width), h: Math.round(dr.height) } : null };
      })()`,
      returnByValue: true,
    });
    const info = opened.result.value || {};
    await new Promise((r) => setTimeout(r, 600));
    const name = id.replace("modal", "");
    shots[name] = await shot(`dialog-${name}.png`);
    result.dialogs[id] = { ...(result.dialogs[id] || {}), opened: info };
    if (!info.ok) result.problems.push("could not open dialog " + id);
    else if (!info.dialog || info.dialog.w < 200 || info.dialog.h < 80) {
      result.problems.push("dialog " + id + " has no usable size " + JSON.stringify(info.dialog));
    }
    await session.send("Runtime.evaluate", {
      expression: `document.getElementById(${JSON.stringify(id)}).classList.remove("open")`,
    });
  }

  // The light theme must be a real second appearance, not a recolour of one variable.
  const themeButton = await session.send("Runtime.evaluate", {
    expression: `(() => {
      const b = document.getElementById("themebtn");
      if (!b) return { ok: false, why: "missing" };
      const r = b.getBoundingClientRect();
      const before = document.documentElement.dataset.theme || "dark";
      const label = b.textContent.trim();
      b.click();
      const after = document.documentElement.dataset.theme;
      let stored = null;
      try { stored = localStorage.getItem("myriaxon-theme"); } catch (e) { stored = "unavailable"; }
      return { ok: true, label, before, after, stored,
               w: Math.round(r.width), h: Math.round(r.height),
               clickable: r.width > 10 && r.height > 10 && r.right <= innerWidth && r.bottom <= innerHeight };
    })()`,
    returnByValue: true,
  });
  result.themeButton = themeButton.result.value || {};
  // Measured while light is active, before switching back: probing after the restore would compare
  // dark against dark and report that the palette never changed.
  const light = await session.send("Runtime.evaluate", { expression: PROBE, returnByValue: true });
  result.light = light.result.value || {};
  await new Promise((r) => setTimeout(r, 700));
  shots.light = await shot("dashboard-light.png");

  // Back to dark, so the file's overall last state matches the theme the dialogs were captured in and
  // a re-run starts from a known appearance.
  await session.send("Runtime.evaluate", {
    expression: `(() => {
      if ((document.documentElement.dataset.theme || "dark") === "light") {
        const b = document.getElementById("themebtn");
        if (b) b.click();
      }
    })()`,
  });
  await new Promise((r) => setTimeout(r, 400));

  result.consoleErrors = consoleErrors;
  if (consoleErrors.length) {
    for (const err of consoleErrors) result.problems.push("page exception: " + String(err).slice(0, 200));
  }

  console.log("RESULT:" + JSON.stringify({ result, shots, chromeLog: chromeLog.slice(-400) }));
  ws.close();
  chrome.kill();
  process.exit(0);
}

main().catch((err) => {
  console.log("RESULT:" + JSON.stringify({ fatal: String(err.message || err), chromeLog: chromeLog.slice(-600) }));
  try { chrome.kill(); } catch {}
  process.exit(2);
});
