/* JARVIS-Kern: animierter Reaktor, der auf den Zustand reagiert. */
(function () {
  const COLORS = {
    idle:      [25, 211, 255],
    listening: [60, 170, 255],
    thinking:  [167, 123, 255],
    executing: [255, 181, 71],
    speaking:  [122, 240, 255],
    halted:    [255, 77, 106],
  };
  const SPEED = { idle: .25, listening: .6, thinking: 1.6, executing: 2.2, speaking: .8, halted: .08 };

  function lerp(a, b, t) { return a + (b - a) * t; }
  function rgba(c, a) { return `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`; }

  class Core {
    constructor(canvas, opts = {}) {
      this.c = canvas; this.g = canvas.getContext("2d");
      this.state = "idle"; this.level = 0; this.target = 0;
      this.col = COLORS.idle.slice(); this.speed = SPEED.idle;
      this.t = 0; this.rot = 0; this.boot = opts.boot ? 0 : 1;
      this.particles = Array.from({ length: 70 }, () => this._p());
      this.ripples = [];
      this.resize(); addEventListener("resize", () => this.resize());
      this.loop = this.loop.bind(this); requestAnimationFrame(this.loop);
    }
    _p() { return { a: Math.random() * Math.PI * 2, r: .55 + Math.random() * .6, s: (Math.random() - .5) * .004, z: Math.random() }; }
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

    loop(ts) {
      requestAnimationFrame(this.loop);
      if (!this.w) return this.resize();
      const g = this.g, W = this.w, H = this.h;
      const dt = 1 / 60; this.t += dt;
      this.boot = Math.min(1, this.boot + dt * .45);
      const tc = COLORS[this.state] || COLORS.idle;
      for (let i = 0; i < 3; i++) this.col[i] = lerp(this.col[i], tc[i], .06);
      this.speed = lerp(this.speed, SPEED[this.state] || .3, .05);
      this.level = lerp(this.level, this.target, .25);
      if (this.state !== "speaking" && this.state !== "listening") this.target *= .9;
      this.rot += this.speed * dt;

      const cx = W / 2, cy = H * .47, R = Math.min(W, H) * .30 * (0.85 + .15 * this._ease(this.boot));
      const col = this.col, lv = this.level, b = this._ease(this.boot);
      g.clearRect(0, 0, W, H);

      // Hintergrund-Glühen
      const glow = g.createRadialGradient(cx, cy, 0, cx, cy, R * 2.2);
      glow.addColorStop(0, rgba(col, .16 + lv * .2)); glow.addColorStop(1, rgba(col, 0));
      g.fillStyle = glow; g.fillRect(0, 0, W, H);

      // Partikel
      for (const p of this.particles) {
        p.a += p.s * (1 + this.speed);
        const rr = R * (p.r + .08 * Math.sin(this.t * .7 + p.z * 9));
        const x = cx + Math.cos(p.a) * rr * 1.5, y = cy + Math.sin(p.a) * rr * 1.5;
        g.fillStyle = rgba(col, (.15 + p.z * .45) * b);
        g.beginPath(); g.arc(x, y, .6 + p.z * 1.4, 0, 7); g.fill();
      }

      g.save(); g.translate(cx, cy);
      g.lineCap = "round";

      // Äußerer Skalenring
      g.save(); g.rotate(this.rot * .15);
      for (let i = 0; i < 120; i++) {
        if (i / 120 > b) break;
        const a = i / 120 * Math.PI * 2, long = i % 10 === 0;
        g.strokeStyle = rgba(col, long ? .7 : .25); g.lineWidth = long ? 2 : 1;
        g.beginPath(); g.moveTo(Math.cos(a) * R * 1.32, Math.sin(a) * R * 1.32);
        g.lineTo(Math.cos(a) * R * (long ? 1.24 : 1.28), Math.sin(a) * R * (long ? 1.24 : 1.28)); g.stroke();
      }
      g.restore();

      // Segmentringe
      const rings = [
        { r: 1.14, w: 3, segs: [[0, .9], [1.1, 2.2], [2.6, 3.0], [3.5, 5.4]], dir: 1, sp: .5, a: .75 },
        { r: 1.03, w: 1.5, segs: [[0, 2.8], [3.3, 6.0]], dir: -1, sp: .8, a: .45 },
        { r: .92, w: 6, segs: [[0, .35], [.8, 1.15], [1.6, 1.95], [2.4, 2.75], [3.2, 3.55], [4.0, 4.35], [4.8, 5.15], [5.6, 5.95]], dir: 1, sp: 1.2, a: .55 },
      ];
      for (const ring of rings) {
        g.save(); g.rotate(this.rot * ring.sp * ring.dir);
        g.strokeStyle = rgba(col, ring.a * b); g.lineWidth = ring.w;
        g.shadowColor = rgba(col, .8); g.shadowBlur = 10;
        for (const [s, e] of ring.segs) { g.beginPath(); g.arc(0, 0, R * ring.r, s, s + (e - s) * b); g.stroke(); }
        g.restore();
      }

      // Denken: kreisende Punkte
      if (this.state === "thinking" || this.state === "executing") {
        for (let i = 0; i < 3; i++) {
          const a = this.rot * 3 + i * 2.094;
          g.fillStyle = rgba(col, .95); g.shadowColor = rgba(col, 1); g.shadowBlur = 16;
          g.beginPath(); g.arc(Math.cos(a) * R * .8, Math.sin(a) * R * .8, 4, 0, 7); g.fill();
        }
        g.shadowBlur = 0;
      }
      // Ausführen: Scanner-Bogen
      if (this.state === "executing") {
        g.strokeStyle = rgba(col, .9); g.lineWidth = 3;
        g.beginPath(); g.arc(0, 0, R * 1.2, this.rot * 4, this.rot * 4 + .9); g.stroke();
      }

      // Wellenform (Zuhören / Sprechen)
      const wave = this.state === "speaking" || this.state === "listening" ? lv : .03;
      g.beginPath();
      const N = 160;
      for (let i = 0; i <= N; i++) {
        const a = i / N * Math.PI * 2;
        const n = Math.sin(a * 6 + this.t * 5) * .5 + Math.sin(a * 11 - this.t * 7) * .3 + Math.sin(a * 17 + this.t * 3) * .2;
        const rr = R * (.68 + n * wave * .22 + wave * .05);
        const x = Math.cos(a) * rr, y = Math.sin(a) * rr;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.strokeStyle = rgba(col, .85 * b); g.lineWidth = 2; g.shadowColor = rgba(col, 1); g.shadowBlur = 14; g.stroke();
      g.shadowBlur = 0;

      // Innerer Kern
      const pulse = 1 + Math.sin(this.t * 2) * .03 + lv * .18;
      const core = g.createRadialGradient(0, 0, 0, 0, 0, R * .55 * pulse);
      core.addColorStop(0, `rgba(255,255,255,${.9 * b})`);
      core.addColorStop(.18, rgba(col, .95 * b));
      core.addColorStop(.5, rgba(col, .28 * b));
      core.addColorStop(1, rgba(col, 0));
      g.fillStyle = core; g.beginPath(); g.arc(0, 0, R * .55 * pulse, 0, 7); g.fill();

      // Dreieck im Kern (Arc Reactor) + Speichen
      g.save(); g.rotate(-this.rot * .6);
      g.strokeStyle = rgba(col, .55 * b); g.lineWidth = 1.4;
      g.beginPath();
      for (let i = 0; i <= 3; i++) {
        const a = i / 3 * Math.PI * 2 - Math.PI / 2;
        const x = Math.cos(a) * R * .4, y = Math.sin(a) * R * .4;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.stroke();
      for (let i = 0; i < 12; i++) {
        const a = i / 12 * Math.PI * 2;
        g.beginPath(); g.moveTo(Math.cos(a) * R * .47, Math.sin(a) * R * .47); g.lineTo(Math.cos(a) * R * .53, Math.sin(a) * R * .53); g.stroke();
      }
      g.beginPath(); g.arc(0, 0, R * .45, 0, 7); g.stroke();
      g.restore();

      // Wellen beim Zuhören
      for (const rp of this.ripples) {
        rp.r += 3; rp.a -= .012;
        g.strokeStyle = rgba(col, Math.max(0, rp.a) * .6); g.lineWidth = 2;
        g.beginPath(); g.arc(0, 0, R * .6 + rp.r, 0, 7); g.stroke();
      }
      this.ripples = this.ripples.filter(r => r.a > 0);
      if (this.state === "listening" && Math.random() < .03 + lv * .1) this.ripples.push({ r: 0, a: .8 });

      g.restore();
    }
    _ease(x) { return 1 - Math.pow(1 - x, 3); }
  }
  window.Core = Core;
})();
