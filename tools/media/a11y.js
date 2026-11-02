// Keyboard and reduced-motion checks on the reasoning graph and the ripple view. Usage: node a11y.js <base url>
const { chromium } = require("playwright");
const assert = (c, m) => { if (!c) { console.log("FAIL", m); process.exitCode = 1; } else console.log("ok  ", m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1400, height: 900 }, reducedMotion: "reduce" });
  const p = await ctx.newPage();
  const errs = []; p.on("pageerror", e => errs.push(e.message)); p.on("console", m => m.type() === "error" && errs.push(m.text()));
  await p.goto(process.argv[2] + "/index.html#ev=sale_60|cuad:RaeSystems@review"); await p.waitForTimeout(1200);
  await p.focus("#rg-0 .rg-scroll");
  await p.keyboard.press("ArrowRight"); await p.keyboard.press("ArrowRight");
  assert(await p.$("#rg-0 .node.sel") !== null, "arrow keys select a step of the trace");
  const sel = await p.$eval("#rg-0 .node.sel", n => n.dataset.node);
  await p.keyboard.press("ArrowLeft");
  assert(await p.$eval("#rg-0 .node.sel", n => n.dataset.node) !== sel, "left arrow moves back");
  for (let i = 0; i < 3; i++) await p.keyboard.press("ArrowRight");
  const atom = await p.$eval("#rg-0 .node.sel", n => n.dataset.node);
  if (atom.startsWith("atom:")) { await p.keyboard.press("Enter");
    assert(await p.evaluate(() => document.activeElement.matches("[data-quote]")), "Enter on a ground opens its clause (focus on the quote)"); }
  await p.focus("#rg-0 .rg-scroll"); await p.keyboard.press("Escape");
  assert(await p.$("#rg-0 .node.sel") === null, "Esc clears the selected step");
  assert(!(await p.$eval("#ev", e => e.hidden)), "Esc on a selection does not close the panel");
  await p.keyboard.press("Escape");
  assert(await p.$eval("#ev", e => e.hidden), "a second Esc closes the evidence panel");
  await p.goto(process.argv[2] + "/index.html#ev=sale_60|cuad:RaeSystems@review"); await p.waitForTimeout(1000);
  await p.click("#rg-0 [data-act=play]"); await p.waitForTimeout(950);
  const anim = await p.$$eval("#rg-0 .edge.lit, #rg-0 .node.now rect.box", xs => xs.map(x => getComputedStyle(x).animationName));
  assert(anim.every(a => a === "none"), `reduced motion: no animation during play (${anim.length} lit elements checked)`);
  const unlabeled = await p.$$eval("#ev button, #ev input, #ev select", xs => xs.filter(x => {
    const t = (x.getAttribute("aria-label") || x.textContent || (x.closest("label") || {}).textContent || "").trim(); return !t; }).map(x => x.outerHTML.slice(0, 80)));
  assert(unlabeled.length === 0, "every control in the evidence panel has a text label " + JSON.stringify(unlabeled.slice(0, 3)));
  const tabbable = await p.$$eval("#ev button, #ev input, #ev select, #ev [tabindex='0']", xs => xs.filter(x => x.tabIndex >= 0 && !x.disabled).length);
  assert(tabbable > 5, `controls reachable with Tab: ${tabbable}`);
  await p.goto(process.argv[2] + "/index.html#ripple=sale_60"); await p.waitForTimeout(800);
  const delays = await p.$$eval(".ripple .dot", xs => xs.map(x => x.style.transitionDelay));
  assert(delays.every(d => d === "0ms"), "reduced motion: the ripple lights without delays");
  await p.focus(".ripple .ct"); await p.keyboard.press("Enter"); await p.waitForTimeout(500);
  assert(!(await p.$eval("#ev", e => e.hidden)), "Enter on a contract in the ripple opens its reasoning graph");
  assert(errs.length === 0, "no console errors " + JSON.stringify(errs));
  await b.close();
})();
