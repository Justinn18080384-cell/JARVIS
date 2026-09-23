/* JARVIS-Kern (Dark & Gold): goldene Kugel mit feinen Ringen, die auf den Zustand reagiert. */
(function () {
  const COLORS = {
    idle:      [212, 175, 55],    // Gold
    listening: [246, 221, 150],   // helles Champagner-Gold
    thinking:  [232, 166, 74],    // Bernstein
    executing: [205, 127, 50],    // Kupfer
    speaking:  [255, 236, 180],   // fast weißes Gold
    halted:    [224, 96, 90],     // Rot
  };
  const SPEED = { idle: .18, listening: .45, thinking: 1.2, executing: 1.7, speaking: .6, halted: .05 };

  function lerp(a, b, t) { return a + (b - a) * t; }
  function rgba(c, a) { return `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`; }

  class Core {
    constructor(canvas, opts = {}) {
      this.c = canvas; this.g = canvas.getContext("2d");
      this.state = "idle"; this.level = 0; this.target = 0;
      this.col = COLORS.idle.slice(); this.speed = SPEED.idle;
      this.t = 0; this.rot = 0; this.boot = opts.boot ? 0 : 1;
      this.dust = Array.from({ length: 90 }, () => this._dust());
      this.ripples = [];
      this.resize(); addEventListener("resize", () => this.resize());
      this.loop = this.loop.bind(this); requestAnimationFrame(this.loop);
    }
    _dust() { return { a: Math.random() * Math.PI * 2, r: .5 + Math.random() * .9, s: (Math.random() - .5) * .003, z: Math.random(), tw: Math.random() * 6 }; }
    resize() {
      const r = this.c.getBoundingClientRect(), d = devicePixelRatio || 1;
      this.c.width = Math.max(1, r.width * d); this.c.height = Math.max(1, r.height * d);
      this.g.setTransform(d, 0, 0, d, 0, 0); this.w = r.width; this.h = r.height;
    }
    setState(s) {
      if (s === this.state) return;
      this.state = s;
      if (s === "listening") this.ripples.push({ r: 0, a: 1 });
    }
    setLevel(l) { this.target = Math.max(0, Math.min(1, l)); }

    loop() {
      requestAnimationFrame(this.loop);
      if (!this.w) return this.resize();
      const g = this.g, W = this.w, H = this.h;
      const dt = 1 / 60; this.t += dt;
      this.boot = Math.min(1, this.boot + dt * .4);
      const tc = COLORS[this.state] || COLORS.idle;
      for (let i = 0; i < 3; i++) this.col[i] = lerp(this.col[i], tc[i], .05);
      this.speed = lerp(this.speed, SPEED[this.state] || .2, .05);
      this.level = lerp(this.level, this.target, .25);
      if (this.state !== "speaking" && this.state !== "listening") this.target *= .9;
      this.rot += this.speed * dt;

      const b = this._ease(this.boot);
      const cx = W / 2, cy = H * .46, R = Math.min(W, H) * .28 * (0.9 + .1 * b);
      const col = this.col, lv = this.level;
      g.clearRect(0, 0, W, H);

      // weiches Leuchten
      const glow = g.createRadialGradient(cx, cy, 0, cx, cy, R * 2.4);
      glow.addColorStop(0, rgba(col, (.12 + lv * .16) * b)); glow.addColorStop(1, rgba(col, 0));
      g.fillStyle = glow; g.fillRect(0, 0, W, H);

      // Goldstaub, der langsam kreist und funkelt
      for (const p of this.dust) {
        p.a += p.s * (1 + this.speed * 2);
        const rr = R * p.r * 1.55;
        const x = cx + Math.cos(p.a) * rr, y = cy + Math.sin(p.a) * rr * .92;
        const tw = .5 + .5 * Math.sin(this.t * 1.5 + p.tw);
        g.fillStyle = rgba(col, (.08 + p.z * .4 * tw) * b);
        g.beginPath(); g.arc(x, y, .5 + p.z * 1.2, 0, 7); g.fill();
      }

      g.save(); g.translate(cx, cy); g.lineCap = "round";

      // feine Außenlinie mit goldenen Markierungen (zeichnet sich beim Start)
      g.save(); g.rotate(this.rot * .1);
      g.strokeStyle = rgba(col, .18 * b); g.lineWidth = 1;
      g.beginPath(); g.arc(0, 0, R * 1.36, 0, Math.PI * 2 * b); g.stroke();
      for (let i = 0; i < 4; i++) {
        const a = i * Math.PI / 2;
        g.fillStyle = rgba(col, .7 * b);
        g.save(); g.rotate(a); g.translate(R * 1.36, 0); g.rotate(Math.PI / 4);
        g.fillRect(-2.5, -2.5, 5, 5); g.restore();        // kleine Raute
      }
      g.restore();

      // zwei elegante Ringe mit Lücken, gegenläufig
      const rings = [
        { r: 1.16, w: 1.2, gap: .5, sp: .35, dir: 1, a: .55 },
        { r: 1.04, w: 2.2, gap: 1.1, sp: .6, dir: -1, a: .4 },
      ];
      for (const ring of rings) {
        g.save(); g.rotate(this.rot * ring.sp * ring.dir);
        g.strokeStyle = rgba(col, ring.a * b); g.lineWidth = ring.w;
        g.shadowColor = rgba(col, .6); g.shadowBlur = 8;
        const len = (Math.PI * 2 - ring.gap * 2) / 2;
        for (let k = 0; k < 2; k++) {
          const s = k * Math.PI; g.beginPath(); g.arc(0, 0, R * ring.r, s, s + len * b); g.stroke();
        }
        g.restore();
      }

      // Denken / Ausführen: goldene Funken auf einer Umlaufbahn
      if (this.state === "thinking" || this.state === "executing") {
        const n = this.state === "executing" ? 4 : 3;
        for (let i = 0; i < n; i++) {
          const a = this.rot * 2.6 + i * Math.PI * 2 / n;
          g.fillStyle = rgba(col, .95); g.shadowColor = rgba(col, 1); g.shadowBlur = 14;
          g.beginPath(); g.arc(Math.cos(a) * R * .86, Math.sin(a) * R * .86, 3, 0, 7); g.fill();
        }
        g.shadowBlur = 0;
      }

      // Wellenlinie (Zuhören / Sprechen)
      const wave = this.state === "speaking" || this.state === "listening" ? lv : .025;
      g.beginPath();
      const N = 180;
      for (let i = 0; i <= N; i++) {
        const a = i / N * Math.PI * 2;
        const n = Math.sin(a * 5 + this.t * 4) * .5 + Math.sin(a * 9 - this.t * 6) * .3 + Math.sin(a * 14 + this.t * 2.5) * .2;
        const rr = R * (.72 + n * wave * .2 + wave * .04);
        const x = Math.cos(a) * rr, y = Math.sin(a) * rr;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.strokeStyle = rgba(col, .75 * b); g.lineWidth = 1.4; g.shadowColor = rgba(col, .9); g.shadowBlur = 12; g.stroke();
      g.shadowBlur = 0;

      // die goldene Kugel
      const pulse = 1 + Math.sin(this.t * 1.6) * .025 + lv * .16;
      const rad = R * .5 * pulse;
      const orb = g.createRadialGradient(-rad * .3, -rad * .35, rad * .05, 0, 0, rad);
      orb.addColorStop(0, `rgba(255,248,225,${.95 * b})`);
      orb.addColorStop(.25, rgba([Math.min(255, col[0] + 30), Math.min(255, col[1] + 40), Math.min(255, col[2] + 60)], .9 * b));
      orb.addColorStop(.7, rgba(col, .55 * b));
      orb.addColorStop(1, rgba([col[0] * .45, col[1] * .4, col[2] * .3], .0));
      g.fillStyle = orb; g.beginPath(); g.arc(0, 0, rad, 0, 7); g.fill();
      // feiner Glanzring um die Kugel
      g.strokeStyle = rgba(col, .35 * b); g.lineWidth = 1;
      g.beginPath(); g.arc(0, 0, rad * 1.02, 0, 7); g.stroke();

      // Wellen beim Zuhören
      for (const rp of this.ripples) {
        rp.r += 2.2; rp.a -= .012;
        g.strokeStyle = rgba(col, Math.max(0, rp.a) * .45); g.lineWidth = 1.2;
        g.beginPath(); g.arc(0, 0, R * .6 + rp.r, 0, 7); g.stroke();
      }
      this.ripples = this.ripples.filter(r => r.a > 0);
      if (this.state === "listening" && Math.random() < .025 + lv * .08) this.ripples.push({ r: 0, a: .7 });

      g.restore();
    }
    _ease(x) { return 1 - Math.pow(1 - x, 3); }
  }
  window.Core = Core;
})();
