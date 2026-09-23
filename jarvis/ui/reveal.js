/* Aufbau-Animationen: Beim Öffnen einer Seite erscheinen Überschrift, Karten und Einträge
   nacheinander, Goldlinien zeichnen sich, Zahlen zählen hoch.
   Nur beim Betreten einer Seite – spätere Aktualisierungen (z. B. Dashboard alle 4 s) bleiben ruhig. */
(function () {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const ITEMS = [".toolbar", ".page > .hint", ".card", ".auto-card", ".row", ".log .e", ".empty", ".core-panel", ".chat-panel", ".msg", ".device"].join(",");
  const MAX_STAGGER = 16;

  const Reveal = {
    on: true,
    enabled() { return this.on && !reduce.matches; },

    /** Titelleiste und Navigation beim Programmstart nacheinander einblenden. */
    intro() {
      if (!this.enabled()) return;
      const bits = [...document.querySelectorAll("#titlebar .brand, #titlebar .status-pill, #titlebar .win-buttons")];
      bits.forEach((el, i) => this._mark(el, "rv", i, 0));
      [...document.querySelectorAll("#nav button")].forEach((el, i) => this._mark(el, "rv-side", i, 150));
    },

    /** Eine Seite aufbauen. */
    page(section, base = 0) {
      if (!section || !this.enabled()) return;
      const h1 = section.querySelector("h1");
      if (h1) this.title(h1);
      const items = [...section.querySelectorAll(ITEMS)].filter(el => el.offsetParent !== null && !el.closest(".rv-skip"));
      // Verschachtelte Elemente (Zeilen in einer Karte) nicht zusätzlich animieren – die Karte trägt sie
      const top = items.filter(el => !items.some(o => o !== el && o.contains(el)));
      top.forEach((el, i) => this._mark(el, "rv", Math.min(i, MAX_STAGGER), base + (h1 ? 180 : 0)));
      this.count(section);
    },

    /** Überschrift: Buchstaben erscheinen einzeln, danach die Goldlinie. */
    title(h1) {
      const small = h1.querySelector("small");
      const text = [...h1.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join("").trim();
      if (!text) return;
      h1.textContent = "";
      [...text].forEach((ch, i) => {
        const s = document.createElement("span"); s.className = "rv-ch"; s.style.setProperty("--c", i); s.textContent = ch; h1.appendChild(s);
      });
      if (small) { h1.appendChild(document.createTextNode(" ")); h1.appendChild(small); this._mark(small, "rv", 0, text.length * 32 + 100); }
      h1.style.setProperty("--line-delay", `${text.length * 32 + 200}ms`);
      h1.classList.remove("rv-title"); void h1.offsetWidth; h1.classList.add("rv-title");
    },

    /** Zahlen (Prozent, Euro, …) von 0 hochzählen und einmal golden aufglänzen lassen. */
    count(root) {
      root.querySelectorAll(".gauge .val, .cost-stats b, [data-count]").forEach(el => {
        const txt = el.textContent, m = txt.match(/-?\d+(?:[.,]\d+)?/);
        if (!m) return;
        const target = parseFloat(m[0].replace(",", ".")), dec = (m[0].split(/[.,]/)[1] || "").length, sep = m[0].includes(",") ? "," : ".";
        const t0 = performance.now(), dur = 900;
        el.classList.remove("rv-num"); void el.offsetWidth; el.classList.add("rv-num");
        const step = now => {
          if (!el.isConnected) return;
          const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3);
          el.textContent = txt.replace(m[0], (target * e).toFixed(dec).replace(".", sep));
          if (p < 1) requestAnimationFrame(step); else el.textContent = txt;
        };
        requestAnimationFrame(step);
      });
    },

    _mark(el, cls, i, base) {
      el.classList.remove("rv", "rv-side");
      el.style.setProperty("--i", i);
      el.style.setProperty("--rv-base", base + "ms");
      void el.offsetWidth;          // Animation neu starten
      el.classList.add(cls);        // bleibt stehen: Endzustand ist „normal“, und die Goldlinie der Karte (::before) läuft zu Ende
    },
  };
  window.Reveal = Reveal;
})();
