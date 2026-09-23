// Mock der pywebview-API für die visuelle Prüfung im Browser
(function () {
  const cfg = {
    setup_done: true, user_name: "Justin",
    ai: { enabled: true, provider: "openai", model: "gpt-4.1-mini", ollama_url: "http://localhost:11434", ollama_model: "", web_search: false },
    briefing: { city: "", include_news: true },
    voice: { tts_enabled: true, tts_engine: "edge", edge_voice: "de-DE-ConradNeural", openai_voice: "onyx", elevenlabs_voice: "onwK4e9ZLuTAKqWW03F9", volume: 90, rate: 0,
      stt_engine: "openai", local_stt_model: "small", wake_word: true, wake_threshold: .5, always_listen: false, silence_seconds: 1.8, max_record_seconds: 30,
      input_device: null, output_device: null, chime: true },
    security: { confirm_level: 2, privacy_mode: false, screen_ai: true },
    focus: { mode: "gaming", auto: true, sleep: { enabled: false, from: "23:00", to: "07:00" } },
    app: { start_with_windows: false, start_minimized: false, close_to_tray: true, boot_animation: true, notifications: true, active_mode: true, auto_backup: true },
    update: { url: "", auto_check: true }, remote: { enabled: false, port: 8765, token: "" }, smarthome: { homeassistant_url: "" },
  };
  const now = Date.now() / 1000;
  const status = { version: "1.0.0", halted: false, modules: { ai: { available: true, model: "gpt-4.1-mini", requests: 3, has_key: true },
    voice: { wake: true, tts: "edge", stt: "openai" }, pc: { apps: 198 }, automation: { profiles: 1, routines: 3, commands: 1 },
    smarthome: { on: 1 }, phone: { devices: [] }, remote: { enabled: false },
    focus: { mode: "gaming", label: "Gaming", manual: false, auto: true, missed: 3 } } };
  const api = {
    ready: async () => ({ version: "1.0.0", config: cfg, has_key: true, status, background: false }),
    state: async () => ({ version: "1.0.0", config: cfg, has_key: true, status, background: false }),
    send: async t => { setTimeout(() => JARVIS.event("state", { state: "thinking" }), 100); setTimeout(() => JARVIS.event("reply", { text: "Discord wird gestartet." }), 900); setTimeout(() => JARVIS.event("state", { state: "idle" }), 1000); return true; },
    toggle_mic: async () => true, stop_speaking: async () => true, emergency_stop: async () => true, resume: async () => true, js_error: () => {},
    dashboard: async () => ({ system: { cpu: 23, ram: 51, ram_used: 16.4e9, ram_total: 32e9, cpu_name: "AMD Ryzen 7 5800X", cores: 16, host: "GAMING-PC",
      os: "Windows 11", uptime: 23000, procs: 312, disks: [{ mount: "C:\\", used: 700e9, total: 1000e9, percent: 70 }, { mount: "E:\\", used: 1.6e12, total: 2e12, percent: 81 }],
      gpu: { name: "NVIDIA GeForce RTX 3070", load: 7, temp: 49 }, battery: null }, status, memory_count: 4,
      missed: [{ ts: now - 60, priority: 0, title: "Laufwerk", text: "Neues Laufwerk E:\\ erkannt." }, { ts: now - 400, priority: 1, title: "Routine", text: "Backup-Routine ausgeführt." }],
      costs: { today: 0.031, month: 0.84, prev_month: 1.27, all: 2.11, all_n: 412,
        days: [...Array(30)].map((_, i) => ({ day: new Date(Date.now() - (29 - i) * 864e5).toLocaleDateString("sv-SE"), eur: i % 4 ? Math.random() * 0.06 : 0 })),
        by_kind: [{ service: "OpenAI", kind: "Chat", n: 180, units: 412000, unit: "Tokens", eur: 0.61 }, { service: "OpenAI", kind: "Spracherkennung", n: 150, units: 690, unit: "Sekunden", eur: 0.03 },
          { service: "OpenAI", kind: "Briefing & Recherche", n: 6, units: 38000, unit: "Tokens", eur: 0.19 }, { service: "ElevenLabs", kind: "Stimme", n: 90, units: 6120, unit: "Zeichen", eur: 0 }],
        elevenlabs_chars: 6120, elevenlabs_free: 10000 }, automations: 5, rooms: 2, devices: 3,
      activity: [{ ts: now - 30, kind: "pc", text: "Startet ein Programm (name=Discord) → Discord wird gestartet.", ok: 1 }, { ts: now - 90, kind: "gedächtnis", text: "Speichert eine Information → Gemerkt: Dein Lieblingsspiel ist GTA.", ok: 1 }] }),
    memory_list: async () => [{ key: "lieblingsspiel", label: "Lieblingsspiel", value: "GTA V", updated: now - 100 }, { key: "name", label: "Name", value: "Justin", updated: now - 5000 }],
    automations: async () => [
      { id: "a1", type: "command", name: "FiveM", enabled: true, triggers: ["FiveM"], steps: [{ text: "starte Discord", calls: [1] }, { text: "starte TeamSpeak", calls: [1], delay: 5 }, { text: "starte FiveM", calls: [1], delay: 5 }] },
      { id: "a2", type: "routine", name: "Ich gehe schlafen", enabled: true, triggers: ["Ich gehe schlafen", "Gute Nacht"], steps: [{ text: "Ton aus", calls: [1] }, { text: "sperre den PC", calls: [1] }], schedule: { time: "23:30", days: [0, 1, 2, 3, 4, 5, 6] } },
      { id: "a3", type: "profile", name: "GTA", enabled: true, triggers: ["GTA"], steps: [{ text: "starte Steam", calls: [1] }, { text: "öffne das Rockstar Social Club Menü", calls: [] }] }],
    smarthome: async () => ({ rooms: ["Wohnzimmer", "Schlafzimmer"], devices: [{ id: "d1", name: "Deckenlicht", room: "Wohnzimmer", type: "light", provider: "virtual", state: "on" },
      { id: "d2", name: "TV-Steckdose", room: "Wohnzimmer", type: "plug", provider: "homeassistant", state: "off" }, { id: "d3", name: "Nachttischlampe", room: "Schlafzimmer", type: "light", provider: "virtual", state: "off" }], ha_url: "", ha_token: false }),
    phone: async () => ({ adb: false, devices: [], remote: { enabled: false, port: 8765, token: "Xy3...demo" } }),
    activity: async () => [{ ts: now - 30, kind: "pc", text: "Startet ein Programm (name=Discord) → Discord wird gestartet.", ok: 1 }, { ts: now - 60, kind: "fehler", text: "KI-Anfrage: Keine Verbindung", ok: 0 }],
    backups: async () => [{ file: "JARVIS-Backup_2026-09-23_07-00-00_automatisch.zip", size: 42000 }],
    diagnose: async () => [{ module: "Gedächtnis", name: "Datenbank", ok: true, msg: "in Ordnung" }, { module: "Sprache", name: "Mikrofon", ok: true, msg: "GXT 256 – Pegel 210" }, { module: "KI", name: "OpenAI", ok: false, msg: "Kein API-Schlüssel" }],
    voices: async () => ({ edge: [["de-DE-ConradNeural", "Conrad (männlich, ruhig)"], ["de-DE-KatjaNeural", "Katja (weiblich)"]], openai: ["onyx", "echo"],
      elevenlabs: [["onwK4e9ZLuTAKqWW03F9", "Daniel (männlich, britisch, ruhig)"], ["JBFqnCBsd6RMkjVDRZzb", "George (männlich, warm)"]], system: [] }),
    set_elevenlabs_key: async k => ({ ok: true, msg: "Verbunden – 24 Stimmen verfügbar." }),
    audio_devices: async () => ({ inputs: ["Mikrofon (GXT 256)"], outputs: ["Lautsprecher (Realtek)", "Kopfhörer"] }),
    models: async () => ["gpt-4.1", "gpt-4.1-mini", "gpt-5-mini"],
    settings_set: async vals => {
      for (const [k, v] of Object.entries(vals || {})) { const p = k.split("."); let n = cfg; p.slice(0, -1).forEach(x => n = n[x] = n[x] || {}); n[p.at(-1)] = v; }
      return { version: "1.0.0", config: cfg, has_key: true, status };
    },
  };
  window.pywebview = { api: new Proxy(api, { get: (t, k) => t[k] || (async () => true) }) };
  setTimeout(() => window.dispatchEvent(new Event("pywebviewready")), 50);
})();
