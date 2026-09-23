/* Lebendige Widgets: Zahlen gleiten zum neuen Wert, Balken/Ringe bewegen sich weich, Verlaufskurven laufen live mit. */
(function () {
  const last = new Map();                       // letzter angezeigter Wert je Schlüssel (überlebt Neuaufbau der Seite)
  const ease = p => 1 - Math.pow(1 - p, 3);
  const NUM = /-?\d+(?:[.,]\d+)?/;

  function parse(txt) {
    const m = String(txt).match(NUM);
    if (!m) return null;
    return { raw: m[0], value: parseFloat(m[0].replace(",", ".")), dec: (m[0].split(/[.,]/)[1] || "").length, sep: m[0].includes(",") ? "," : "." };
  }

  const Live = {
    /** Text eines Elements setzen; enthaltene Zahl gleitet vom alten zum neuen Wert. */
    text(el, txt, key) {
      if (!el) return;
      key = key || el.dataset.live;
      const next = parse(txt), prev = key ? last.get(key) : null;
      if (key && next) last.set(key, next.value);
      if (!next || prev == null || prev === next.value || !Reveal.enabled()) { el.textContent = txt; return; }
      const t0 = performance.now(), dur = 700;
      el._anim = t0;
      // Absicherung: zeichnet das Fenster gerade nicht (minimiert), steht trotzdem der richtige Endwert da
      setTimeout(() => { if (el._anim === t0) { el._anim = 0; el.textContent = txt; } }, dur + 150);
      const step = now => {
        if (el._anim !== t0 || !el.isConnected) return;
        const p = Math.min(1, (now - t0) / dur), v = prev + (next.value - prev) * ease(p);
        el.textContent = p < 1 ? txt.replace(next.raw, v.toFixed(next.dec).replace(".", next.sep)) : txt;
        if (p < 1) requestAnimationFrame(step); else el._anim = 0;
      };
      requestAnimationFrame(step);
    },

    /** Nach einem Neuaufbau (z. B. Dashboard alle 4 s): Zahlen, Balken und Ringe von ihrem alten Stand aus bewegen. */
    after(root) {
      root.querySelectorAll("[data-live]").forEach(el => Live.text(el, el.textContent));
      root.querySelectorAll("[data-live-w]").forEach(el => Live._style(el, el.dataset.liveW, "width"));
      root.querySelectorAll("[data-live-arc]").forEach(el => Live._style(el, el.dataset.liveArc, "strokeDasharray", el.getAttribute("stroke-dasharray")));
    },
    _style(el, key, prop, value) {
      const target = value ?? el.style[prop];
      const prev = last.get(key);
      last.set(key, target);
      if (prev == null || prev === target || !Reveal.enabled()) return;
      if (prop === "strokeDasharray") el.setAttribute("stroke-dasharray", prev);
      el.style[prop] = prev;
      el.style.transition = `${prop === "width" ? "width" : "stroke-dasharray"} .9s cubic-bezier(.16,1,.3,1)`;
      void el.getBoundingClientRect();
      requestAnimationFrame(() => {
        if (prop === "strokeDasharray") el.setAttribute("stroke-dasharray", target);
        el.style[prop] = target;
      });
    },
    reset(prefix) { for (const k of [...last.keys()]) if (!prefix || k.startsWith(prefix)) last.delete(k); },
  };

  /** Goldene Verlaufskurve, die bei jedem neuen Wert weich nach links weiterläuft. */
  class Spark {
    constructor(canvas, max = 40) { this.c = canvas; this.max = max; this.data = []; this.shift = 1; this.t0 = 0; }
    push(v) {
      v = Math.max(0, Math.min(100, v));
      if (!this.data.length) this.data = Array(this.max).fill(v);   // Start mit flacher Linie statt leerem Feld
      this.data.push(v);
      if (this.data.length > this.max + 1) this.data.shift();
      this.t0 = performance.now(); this.shift = 0;
      if (!this._run) { this._run = true; requestAnimationFrame(this._draw.bind(this)); }
    }
    _draw(now) {
      const c = this.c;
      if (!c.isConnected) { this._run = false; return; }
      const r = c.getBoundingClientRect(), d = devicePixelRatio || 1;
      if (c.width !== Math.round(r.width * d)) { c.width = Math.round(r.width * d); c.height = Math.round(r.height * d); }
      const g = c.getContext("2d"), W = c.width, H = c.height, pts = this.data;
      this.shift = Reveal.enabled() ? Math.min(1, (now - this.t0) / 900) : 1;
      g.clearRect(0, 0, W, H);
      if (pts.length > 1) {
        const step = W / (this.max - 1), off = (1 - ease(this.shift)) * step;
        const x = i => W - (pts.length - 1 - i) * step + off, y = v => H - 2 * d - (v / 100) * (H - 4 * d);
        g.beginPath(); g.moveTo(x(0), y(pts[0]));
        for (let i = 1; i < pts.length; i++) {
          const xm = (x(i - 1) + x(i)) / 2;
          g.bezierCurveTo(xm, y(pts[i - 1]), xm, y(pts[i]), x(i), y(pts[i]));
        }
        const fill = g.createLinearGradient(0, 0, 0, H);
        fill.addColorStop(0, "rgba(212,175,55,.35)"); fill.addColorStop(1, "rgba(212,175,55,0)");
        g.strokeStyle = "#f1d88f"; g.lineWidth = 1.4 * d; g.shadowColor = "rgba(212,175,55,.8)"; g.shadowBlur = 6 * d; g.stroke();
        g.shadowBlur = 0; g.lineTo(x(pts.length - 1), H); g.lineTo(x(0), H); g.closePath(); g.fillStyle = fill; g.fill();
        // leuchtender Punkt am aktuellen Wert
        g.fillStyle = "#fff4d0"; g.beginPath(); g.arc(x(pts.length - 1), y(pts[pts.length - 1]), 2.2 * d, 0, 7); g.fill();
      }
      if (this.shift < 1) requestAnimationFrame(this._draw.bind(this)); else this._run = false;
    }
  }

  window.Live = Live;
  window.Spark = Spark;
})();
