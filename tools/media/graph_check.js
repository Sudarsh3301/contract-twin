// Drawer graphs on four events and every agreement: an arrow on every line, a gate on every exception, click lights causes and results, no engine words. Usage: node graph_check.js <base url>
const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch({ channel: "chrome" });
  const p = await (await b.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errs = []; p.on("pageerror", e => errs.push(e.message)); p.on("console", m => m.type() === "error" && errs.push(m.text()));
  await p.goto(process.argv[2] + "/index.html"); await p.waitForTimeout(800);
  const keys = await p.evaluate(() => ({sc: D.scenarios.map(s => s.id), ak: Object.keys(AG), ver: Object.fromEntries(Object.entries(AG).map(([k, a]) => [k, a.versions[0]]))}));
  const bad = /\b(true|false|stated|given|firm|atoms?|NEEDS_LAWYER)\b|=|[A-Za-z0-9]+_[A-Za-z0-9]+/i;
  let n = 0, graphs = 0, noArrow = 0, noGate = 0, noClick = 0, words = [];
  for (const sc of ["sale_60", "ipo_200_60", "sale_30", "sale_10_comp"]) for (const ak of keys.ak) {
    await p.evaluate(h => { location.hash = h; }, `ev=${sc}|${keys.ver[ak]}`); await p.waitForTimeout(120);
    const open = await p.$("#reasoning summary"); if (!open) continue;
    await p.evaluate(() => { const d = document.getElementById("reasoning"); d.open = true; d.dispatchEvent(new Event("toggle")); });
    await p.waitForTimeout(250); n++;
    const r = await p.evaluate(async () => {
      const out = [];
      for (const g0 of document.querySelectorAll(".rg")) { const el = g0.querySelector("svg.graph"); if (!el) continue;
        const edges = el.querySelectorAll(".edge").length, arrows = el.querySelectorAll(".arrow").length;
        const exc = [...el.querySelectorAll(".node.k-exception")].map(x => x.dataset.node);
        const outs = new Set([...el.querySelectorAll(".gate")].length ? [] : []);
        const res = [...el.querySelectorAll(".node.k-outcome")];
        let clicked = null;
        if (res.length) { res[res.length - 1].dispatchEvent(new MouseEvent("click", { bubbles: true })); await new Promise(r => setTimeout(r, 30));
          const g = g0; const s2 = g.querySelector("svg.graph"); clicked = [s2.querySelectorAll(".edge.hit").length, s2.querySelectorAll(".node.fade").length, s2.querySelectorAll(".node").length];
          const q = g.querySelector(".rg-scroll"); q.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); }
        out.push({ edges, arrows, exc: exc.length, gates: el.querySelectorAll(".gate").length, clicked, text: el.textContent + " " + (g0.querySelector(".rg-pane")?.innerText||"") });
      }
      return out;
    });
    for (const g of r) { graphs++; if (g.edges !== g.arrows) noArrow++; if (g.exc && !g.gates) noGate++; if (g.clicked && !(g.clicked[0] > 0 || g.clicked[2] < 3)) noClick++; const m = g.text.match(bad); if (m) words.push([sc, ak, m[0]]); }
  }
  console.log({ drawers: n, graphs, noArrow, noGate, noClick, nwords: words.length, kinds: [...new Set(words.map(w => w[2]))], sample: words.slice(0,3), errs: errs.slice(0, 3) });
  await b.close();
})();
