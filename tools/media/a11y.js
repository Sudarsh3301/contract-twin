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
}

async function audit(b) {
  const src = fs.readFileSync(__dirname + "/audit_page.js", "utf8");
  const screens = [["overview", "#ripple=sale_60", null], ["drawer EN", "#ev=sale_60|cuad:iPayment", "#ev"], ["drawer AR", "#ev=sale_60|ar:mt_ipayment", "#ev"],
                   ["to resolve", "#queue", null], ["analyst", "#map", null]];
  for (const [w, h] of [[1440, 900], [390, 844]]) for (const theme of ["light", "dark"]) {
    const ctx = await b.newContext({ viewport: { width: w, height: h }, colorScheme: theme });
    const p = await ctx.newPage();
    for (const [name, hash, root] of screens) {
      await p.goto(base + "/index.html" + hash); await p.waitForTimeout(900);
      if (root) { await openReasoning(p); await p.waitForTimeout(900); }
      const r = await p.evaluate(`window.AUDIT_ROOT = ${root ? `document.querySelector("${root}")` : "null"}; ${src}`);
      const tag = `${w}px ${theme} ${name}`;
      assert(r.fails === 0, `${tag}: ${r.checked} text nodes meet the contrast minimum; lowest 5: ` + r.lowest.map(x => `${x.ratio} "${x.text}"`).join(", "));
      assert(r.small.length === 0, `${tag}: no HTML text below 12 px ${JSON.stringify(r.small.slice(0, 3))}`);
      if (w === 1440) assert(r.svgSmall.length === 0, `${tag}: no drawn label below 12 px rendered height ${JSON.stringify(r.svgSmall.slice(0, 3))}`);
      assert(r.docW === r.vw, `${tag}: page width ${r.docW} equals viewport width ${r.vw}`);
      if (name === "overview") assert(r.engineWords.length === 0, `${tag}: no engine words on the first screen ${JSON.stringify(r.engineWords)}`);
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
