// Keyboard, reduced-motion, contrast, size and wording checks on the demo page.
//   node a11y.js <base url>          keyboard and reduced-motion checks on the reasoning graph and the ripple
//   node a11y.js <base url> audit    contrast, text size, page width and engine words, per screen, theme and width
const { chromium } = require("playwright");
const fs = require("fs");
const assert = (c, m) => { if (!c) { console.log("FAIL", m); process.exitCode = 1; } else console.log("ok  ", m); };
const base = process.argv[2], mode = process.argv[3] || "keys";
const openReasoning = p => p.evaluate(() => document.querySelectorAll("#ev details.reasoning").forEach(d => { d.open = true; }));

async function keys(b) {
  const ctx = await b.newContext({ viewport: { width: 1400, height: 900 }, reducedMotion: "reduce" });
  const p = await ctx.newPage();
  const errs = []; p.on("pageerror", e => errs.push(e.message)); p.on("console", m => m.type() === "error" && errs.push(m.text()));
  await p.goto(base + "/index.html#ev=sale_60|cuad:RaeSystems@review"); await p.waitForTimeout(1200);
  await openReasoning(p); await p.waitForTimeout(600);
  await p.focus("#rg-0 .rg-scroll");
  await p.keyboard.press("ArrowRight"); await p.keyboard.press("ArrowRight");
  assert(await p.$("#rg-0 .node.sel") !== null, "arrow keys select a step of the trace");
  const sel = await p.$eval("#rg-0 .node.sel", n => n.dataset.node);
  await p.keyboard.press("ArrowLeft");
  assert(await p.$eval("#rg-0 .node.sel", n => n.dataset.node) !== sel, "left arrow moves back");
  for (let i = 0; i < 3; i++) await p.keyboard.press("ArrowRight");
  const atom = await p.$eval("#rg-0 .node.sel", n => n.dataset.node);
  if (atom.startsWith("atom:")) { await p.keyboard.press("Enter");
    assert(await p.evaluate(() => document.activeElement.matches("[data-quote]")), "Enter on a clause fact opens its clause (focus on the quote)"); }
  await p.focus("#rg-0 .rg-scroll"); await p.keyboard.press("Escape");
  assert(await p.$("#rg-0 .node.sel") === null, "Esc clears the selected step");
  assert(!(await p.$eval("#ev", e => e.hidden)), "Esc on a selection does not close the panel");
  await p.keyboard.press("Escape");
  assert(await p.$eval("#ev", e => e.hidden), "a second Esc closes the drawer");
  await p.goto(base + "/index.html#ev=sale_60|cuad:RaeSystems@review"); await p.waitForTimeout(1000);
  await openReasoning(p); await p.waitForTimeout(400);
  await p.click("#rg-0 [data-act=play]"); await p.waitForTimeout(950);
  const anim = await p.$$eval("#rg-0 .edge.lit, #rg-0 .node.now rect.box", xs => xs.map(x => getComputedStyle(x).animationName));
  assert(anim.every(a => a === "none"), `reduced motion: no animation during play (${anim.length} lit elements checked)`);
  const unlabeled = await p.$$eval("#ev button, #ev input, #ev select", xs => xs.filter(x => {
    const t = (x.getAttribute("aria-label") || x.textContent || (x.closest("label") || {}).textContent || "").trim(); return !t; }).map(x => x.outerHTML.slice(0, 80)));
  assert(unlabeled.length === 0, "every control in the drawer has a text label " + JSON.stringify(unlabeled.slice(0, 3)));
  const tabbable = await p.$$eval("#ev button, #ev input, #ev select, #ev [tabindex='0']", xs => xs.filter(x => x.tabIndex >= 0 && !x.disabled).length);
  assert(tabbable > 5, `controls reachable with Tab: ${tabbable}`);
  await p.goto(base + "/index.html#ripple=sale_60"); await p.waitForTimeout(800);
  assert(await p.$$eval(".ripple .wave", xs => xs.every(x => getComputedStyle(x).animationName === "none")), "reduced motion: the ripple wave does not animate");
  await p.focus(".ripple .ct"); await p.keyboard.press("Enter"); await p.waitForTimeout(500);
  assert(!(await p.$eval("#ev", e => e.hidden)), "Enter on an agreement in the ripple opens its drawer");
  await p.goto(base + "/index.html#ripple=sale_60"); await p.waitForTimeout(600);
  await p.focus("[data-none]"); await p.keyboard.press("Enter");
  assert(await p.$$eval(".ripple .dotn", xs => xs.length) > 0, "Enter on the “no reaction” ring shows those agreements");
  assert(errs.length === 0, "no console errors " + JSON.stringify(errs));
  await ctx.close();
  await decideKeys(b);
}

// "How it decides": keyboard reaches the buttons, the phrase and rule linking and the graph toggle; no engine words in any scenario.
async function decideKeys(b) {
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const ctx = await b.newContext({ viewport: { width: w, height: h }, reducedMotion: "reduce" });
    const p = await ctx.newPage();
    const errs = []; p.on("pageerror", e => errs.push(e.message)); p.on("console", m => m.type() === "error" && errs.push(m.text()));
    await p.goto(base + "/index.html#decide"); await p.waitForTimeout(900);
    const tag = `${w}px how it decides`;
    await p.focus("[data-hd='1']"); await p.keyboard.press("Enter"); await p.waitForTimeout(200);
    assert(await p.$eval("[data-hd='1']", e => e.getAttribute("aria-pressed")) === "true", `${tag}: Enter on a scenario button selects it`);
    assert(await p.evaluate(() => document.activeElement.matches("[data-hd='1']")), `${tag}: focus stays on the button after it is chosen`);
    await p.focus("[data-hd='0']"); await p.keyboard.press("Space"); await p.waitForTimeout(200);
    await p.focus(".hd-clause .pt[data-part=stake]");
    assert(await p.$$eval(".hd-rules .pt[data-part=stake].on", xs => xs.length) === 1, `${tag}: focusing a phrase in the clause lights its rule words`);
    await p.focus(".hd-rules li[data-line='3']");
    assert(await p.$$eval(".hd-clause .pt.on", xs => xs.map(x => x.dataset.part).sort().join()) === "consent,end", `${tag}: focusing a rule line lights its phrases in the clause`);
    await p.focus(".hd-graph summary"); await p.keyboard.press("Enter"); await p.waitForTimeout(700);
    assert(await p.$$eval("#hd-graph svg.graph .node", xs => xs.length) > 5, `${tag}: Enter on the graph toggle opens the graph`);
    assert(await p.$eval("#hd-graph .rg-scroll", e => e.tabIndex) === 0, `${tag}: the graph can be reached with Tab`);
    const bad = /(firm|provisional|blocked|atoms?|NEEDS_LAWYER|compiler|simulated)|=|[A-Za-z0-9]+_[A-Za-z0-9]+|cuad:|ar:|\[\d+\]/i;
    for (let i = 0; i < 3; i++) {
      await p.click(`[data-hd='${i}']`); await p.waitForTimeout(500);
      const t = await p.$eval("#view-decide", e => e.innerText);
      assert(!bad.test(t), `${tag}: scenario ${i + 1} shows no engine words, "=", snake_case, contract keys or sentence numbers ${(t.match(bad) || [""])[0]}`);
      assert(await p.$eval("#hd-graph svg.graph", () => true), `${tag}: the graph stays open when the scenario changes`);
      // decision path: every question lights its clause phrase and its rule words, on hover and on focus
      const qs = await p.$$eval(".wy [data-parts]", xs => xs.map((x, k) => [k, x.dataset.parts]));
      await p.mouse.move(0, 0);       // a resting pointer must not light other questions while focus scrolls
      for (const [k, parts] of qs) {
        await p.focus(`.wy [data-parts] >> nth=${k}`);
        const on = await p.$$eval(".pt.on", xs => [...new Set(xs.map(x => x.dataset.part))].sort().join(" "));
        assert(on === parts.split(" ").sort().join(" "), `${tag}: scenario ${i + 1} question ${k + 1} lights ${parts} (got ${on})`);
        assert(await p.$$eval(".hd-clause .pt.on", xs => xs.length) > 0 && await p.$$eval(".hd-rules .pt.on", xs => xs.length) > 0, `${tag}: scenario ${i + 1} question ${k + 1} lights the clause phrase and the rule line`);
      }
      // graph: arrows on every line, a gate on every exception, click lights causes and results, Esc clears
      const gc = await p.evaluate(() => ({edges: document.querySelectorAll("#hd-graph .edge").length, arrows: document.querySelectorAll("#hd-graph .arrow").length,
        gates: document.querySelectorAll("#hd-graph .gate").length, exc: [...document.querySelectorAll("#hd-graph .node.k-exception")].length}));
      assert(gc.edges === gc.arrows && gc.edges > 0, `${tag}: scenario ${i + 1}: an arrow on every line ${JSON.stringify(gc)}`);
      assert(gc.gates >= gc.exc && gc.exc > 0, `${tag}: scenario ${i + 1}: a gate mark on every exception ${JSON.stringify(gc)}`);
      const res = await p.$$eval("#hd-graph .node.k-outcome", xs => xs.map(x => x.dataset.node));
      await p.click(`#hd-graph [data-node="${res[res.length - 1]}"]`); await p.waitForTimeout(150);
      const lit = await p.evaluate(() => ({hit: document.querySelectorAll("#hd-graph .edge.hit").length, fade: document.querySelectorAll("#hd-graph .node.fade").length, all: document.querySelectorAll("#hd-graph .node").length}));
      assert(lit.hit > 0 && lit.fade < lit.all, `${tag}: scenario ${i + 1}: clicking a result lights its causes ${JSON.stringify(lit)}`);
      if (res.length > 1) assert(lit.fade > 0, `${tag}: scenario ${i + 1}: clicking one result fades the other effect ${JSON.stringify(lit)}`);
      await p.focus("#hd-graph .rg-scroll"); await p.keyboard.press("Escape"); await p.waitForTimeout(150);
      assert(await p.$$eval("#hd-graph .node.fade, #hd-graph .edge.hit", xs => xs.length) === 0, `${tag}: scenario ${i + 1}: Esc clears the trace`);
    }
    await p.goto(base + "/index.html#ripple=sale_60"); await p.waitForTimeout(700);
    assert(await p.$$eval("#p-decide a[href^='#decide']", xs => xs.length) === 1, `${tag}: the Overview strip links to the page`);
    await p.click("#p-decide a[href^='#decide']"); await p.waitForTimeout(300);
    assert(await p.$eval("#view-decide", e => !e.hidden), `${tag}: the strip link opens the page`);
    await p.click(".hd-foot [data-why]"); await p.waitForTimeout(500);
    assert(await p.$eval("#ev", e => !e.hidden), `${tag}: the agreement name at the bottom opens its drawer`);
    assert(errs.length === 0, `${tag}: no console errors ` + JSON.stringify(errs));
    await ctx.close();
  }
}

async function audit(b) {
  const src = fs.readFileSync(__dirname + "/audit_page.js", "utf8");
  const screens = [["overview", "#ripple=sale_60", null], ["drawer EN", "#ev=sale_60|cuad:iPayment", "#ev"], ["drawer AR", "#ev=sale_60|ar:mt_ipayment", "#ev"],
                   ["to resolve", "#queue", null], ["analyst", "#map", null], ["how it decides", "#decide", "#view-decide"]];
  for (const [w, h] of [[1440, 900], [390, 844]]) for (const theme of ["light", "dark"]) {
    const ctx = await b.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
    const p = await ctx.newPage();
    for (const [name, hash, root] of screens) {
      await p.goto(base + "/index.html" + hash); await p.waitForTimeout(900);
      if (root) { await openReasoning(p); await p.waitForTimeout(900); }
      if (name === "how it decides") { await p.evaluate(() => { document.querySelector(".hd-graph").open = true; }); await p.waitForTimeout(700); }
      const r = await p.evaluate(`window.AUDIT_ROOT = ${root ? `document.querySelector("${root}")` : "null"}; ${src}`);
      const tag = `${w}px ${theme} ${name}`;
      assert(r.fails === 0, `${tag}: ${r.checked} text nodes meet the contrast minimum; lowest 5: ` + r.lowest.map(x => `${x.ratio} "${x.text}"`).join(", "));
      assert(r.small.length === 0, `${tag}: no HTML text below 12 px ${JSON.stringify(r.small.slice(0, 3))}`);
      if (w === 1440) assert(r.svgSmall.length === 0, `${tag}: no drawn label below 12 px rendered height ${JSON.stringify(r.svgSmall.slice(0, 3))}`);
      assert(r.docW === r.vw, `${tag}: page width ${r.docW} equals viewport width ${r.vw}`);
      if (name === "overview" || name === "how it decides") assert(r.engineWords.length === 0, `${tag}: no engine words on the first screen ${JSON.stringify(r.engineWords)}`);
    }
    await p.goto(base + "/index.html#ripple=sale_60"); await p.waitForTimeout(500);
    const cs = await p.$eval("#view-ripple", () => getComputedStyle(document.documentElement).colorScheme);
    assert(cs.includes(theme), `${theme}: color-scheme is ${cs}, so native controls follow the theme`);
    await ctx.close();
  }
}

(async () => {
  const b = await chromium.launch(process.env.CHROME ? { channel: "chrome" } : {});
  await (mode === "audit" ? audit : keys)(b);
  await b.close();
})();
