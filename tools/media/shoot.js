// Screenshots and short recordings of the demo page. Usage: node shoot.js <base url> <out dir> <plan json>
// A plan item: {name, hash, w, h (default 1500 x 900), dark, reduced, video, steps: [{click, press, focus, fill, select, range, eval, wait, shot}]}.
// Set CHROME=1 to use the installed Chrome instead of Playwright's own browser.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const [base, outDir, planFile] = process.argv.slice(2);
const plan = JSON.parse(fs.readFileSync(planFile, "utf8"));
(async () => {
  const browser = await chromium.launch(process.env.CHROME ? { channel: "chrome" } : {});
  const errors = [];
  for (const item of plan) {
    const size = { width: item.w || 1500, height: item.h || 900 };
    const ctx = await browser.newContext({ viewport: size, deviceScaleFactor: 1,
      colorScheme: item.dark ? "dark" : "light", reducedMotion: item.reduced ? "reduce" : "no-preference",
      recordVideo: item.video ? { dir: outDir + "/raw_" + item.name, size } : undefined });
    const page = await ctx.newPage();
    page.on("console", m => { if (m.type() === "error") errors.push(item.name + ": " + m.text()); });
    page.on("pageerror", e => errors.push(item.name + ": " + e.message));
    page.on("request", r => { if (!r.url().startsWith(base)) errors.push(item.name + ": external request " + r.url()); });
    await page.goto(base + "/index.html#" + item.hash);
    await page.waitForTimeout(800);
    for (const s of item.steps || []) {
      if (s.click) await page.click(s.click);
      if (s.press) await page.keyboard.press(s.press);
      if (s.focus) await page.focus(s.focus);
      if (s.fill) await page.fill(s.fill[0], s.fill[1]);
      if (s.select) await page.selectOption(s.select[0], s.select[1]);
      if (s.range) await page.$eval(s.range[0], (el, v) => { el.value = v; el.dispatchEvent(new Event("input", { bubbles: true })); }, s.range[1]);
      if (s.eval) await page.evaluate(s.eval);
      if (s.wait) await page.waitForTimeout(s.wait);
      if (s.shot) { fs.mkdirSync(path.dirname(path.join(outDir, s.shot)), { recursive: true }); await page.screenshot({ path: path.join(outDir, s.shot), fullPage: false }); }
    }
    await ctx.close();
  }
  await browser.close();
  fs.writeFileSync(outDir + "/errors.txt", errors.join("\n"));
  console.log("done; errors:", errors.length);
})();
