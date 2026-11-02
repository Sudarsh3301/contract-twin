// In-page audit of the demo page: text contrast, text size, page width and engine words on the first screen.
// An expression that returns a result object. a11y.js runs it under Playwright (`node a11y.js <base url> audit`);
// it also runs unchanged in any browser console: eval(await (await fetch("audit_page.js")).text()).
(() => {
  const parse = c => {
    let m = c.match(/rgba?\(([^)]+)\)/);
    if (m) { const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    m = c.match(/color\(srgb ([^)]+)\)/);
    if (m) { const p = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1]; }
    return [0, 0, 0, 0];
  };
  const over = (t, b) => [0, 1, 2].map(i => t[i] * t[3] + b[i] * (1 - t[3])).concat(1);
  const lum = ([r, g, b]) => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }; return .2126 * f(r) + .7152 * f(g) + .0722 * f(b); };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const htmlBg = el => {
    const stack = [];
    for (let e = el; e; e = e.parentElement) {
      const c = parse(getComputedStyle(e).backgroundColor);
      if (c[3] > 0) { stack.push(c); if (c[3] >= 1) break; }
    }
    return stack.reduceRight((base, c) => over(c, base), [255, 255, 255, 1]);
  };
  const svgBg = el => {          // the shape a text sits on, else the page behind the svg
    for (let g = el.closest("g"); g; g = g.parentElement && g.parentElement.closest("g")) {
      for (const s of g.children) {
        if (!/^(rect|circle|polygon)$/.test(s.tagName)) continue;
        const f = parse(getComputedStyle(s).fill);
        if (f[3] > 0) return over(f, htmlBg(g.ownerSVGElement));
      }
    }
    return htmlBg(el.ownerSVGElement);
  };
  const out = {lowest: [], small: [], svgSmall: [], checked: 0, fails: 0, firstScreen: "", docW: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth};
  const all = [];
  const w = document.createTreeWalker(window.AUDIT_ROOT || document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) {
    const t = n.textContent.trim(), el = n.parentElement;
    if (!t || /^(SCRIPT|STYLE|TITLE)$/.test(el.tagName.toUpperCase()) || !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
    const rg = document.createRange(); rg.selectNodeContents(n);
    const rc = rg.getBoundingClientRect();
    if (rc.width === 0 || rc.height === 0) continue;
    const cs = getComputedStyle(el), svg = el.ownerSVGElement, size = parseFloat(cs.fontSize), bold = +cs.fontWeight >= 700;
    const scale = svg ? svg.getBoundingClientRect().width / (svg.viewBox.baseVal.width || svg.getBoundingClientRect().width) : 1;
    const fg = svg ? parse(cs.fill) : parse(cs.color), bg = svg ? svgBg(el) : htmlBg(el);
    const px = size * scale, large = px >= 24 || (px >= 18.66 && bold), r = ratio(over(fg, bg), bg), need = large ? 3 : 4.5;
    const where = (el.closest("[id]") || el).id || el.tagName.toLowerCase();
    all.push({ratio: +r.toFixed(2), need, text: t.slice(0, 40), size: +px.toFixed(1), where, cls: String(el.getAttribute("class") || el.parentElement.getAttribute("class") || "").slice(0, 30), top: rc.top, bottom: rc.bottom});
    if (px < 12) (svg ? out.svgSmall : out.small).push({text: t.slice(0, 40), size: +px.toFixed(1), where});
  }
  out.checked = all.length;
  out.fails = all.filter(x => x.ratio < x.need).length;
  out.lowest = all.slice().sort((a, b) => a.ratio - b.ratio).slice(0, 5);
  out.failing = all.filter(x => x.ratio < x.need).slice(0, 12);
  out.firstScreen = all.filter(x => x.top < innerHeight && x.bottom > 0).map(x => x.text).join(" | ");
  const words = /\b(firm|provisional|blocked|atoms?|grounds?|NOT_TRIGGERED|NEEDS_LAWYER|load-bearing)\b/i, slug = /\b[a-z0-9]+(?:_[a-z0-9]+)+\b/i;
  out.engineWords = [...new Set((out.firstScreen.match(new RegExp(words.source, "gi")) || []).concat(out.firstScreen.match(new RegExp(slug.source, "gi")) || []))];
  return out;
})()
