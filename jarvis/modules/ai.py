"""KI-Anbindung. Wird nur genutzt, wenn der lokale Parser nicht weiterweiß.

Anbieter:
    openai  – OpenAI (Responses API, optional mit Websuche und Bildverständnis)
    ollama  – lokale Modelle über Ollama (http://localhost:11434), komplett offline;
              auch im Datenschutzmodus erlaubt
"""
import base64
import datetime
import json
import re
import threading
import time

import requests

from ..core import log, secrets
from ..core.actions import action, registry, S
from ..core.config import config
from ..core.events import bus
from ..core.module import Module, Intent
from . import costs, memory

M = "ki"
_instance = None

PERSONA = """Du bist JARVIS, der persönliche KI-Assistent des Nutzers auf seinem Windows-PC – höflich, trocken-humorvoll, \
kompetent und knapp wie der Butler-Assistent aus Iron Man. Antworte immer auf Deutsch, in natürlicher gesprochener Sprache, \
meist in 1–3 Sätzen (die Antwort wird vorgelesen: keine Markdown-Formatierung, keine Listen mit Sternchen, keine URLs vorlesen).
Du kannst den PC über Werkzeuge steuern. Nutze sie, wenn der Nutzer etwas getan haben möchte. Frage nach, wenn ein Befehl \
wirklich mehrdeutig ist. Wenn der Nutzer dir etwas Persönliches über sich erzählt, speichere es mit memory_remember. \
Wenn er möchte, dass du auf einen Satz hin etwas tust ("wenn ich X sage, mach Y"), lege mit automation_create einen Befehl an. \
Erfinde keine Fakten über den Nutzer – nutze das Gedächtnis."""
# Lokale Modelle bekommen nur die wichtigsten Werkzeuge: Alle 80+ würden ~6.500 Tokens kosten – mehr als
# Ollamas Standard-Kontext (4.096) – und das Modell langsam und unsicher machen.
OLLAMA_TOOLS = {
    "memory_remember", "memory_recall", "app_open", "app_close", "window_restore", "window_minimize",
    "volume_set", "volume_change", "mute", "media", "pc_lock", "screenshot", "system_info",
    "open_url", "web_search_open", "open_folder", "time_now", "timer",
    "automation_create", "automation_run", "weather", "device_set", "focus_set",
}
# Zusatz für lokale Modelle (Qwen & Co. rutschen sonst gelegentlich ins Russische/Chinesische)
GERMAN_ONLY = ("WICHTIG: Antworte IMMER und AUSSCHLIESSLICH auf Deutsch. Verwende niemals Wörter aus anderen Sprachen "
               "oder andere Schriftzeichen (kein Kyrillisch, kein Chinesisch).")


def _gaming():
    from . import focus
    return focus.current() == "gaming"


class AIModule(Module):
    name = "ai"
    title = "KI"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _instance
        _instance = self
        self._client = None
        self._key = None
        self._ollama_ok = (0, False)
        self.last_error = ""
        self.requests = 0
        self.lock = threading.Lock()

    # ------------------------------------------------------------ Anbieter
    @staticmethod
    def provider():
        """Der in den Einstellungen gewählte Anbieter."""
        return config.get("ai.provider", "openai")

    def active_provider(self):
        """Der gerade tatsächlich genutzte Anbieter: Beim Zocken weicht Ollama auf Wunsch auf OpenAI aus,
        damit das Spiel den Grafikspeicher für sich hat."""
        p = self.provider()
        if (p == "ollama" and _gaming() and config.get("ai.gaming_fallback", "openai") == "openai"
                and secrets.has("openai_api_key") and not config.get("security.privacy_mode")):
            return "openai"
        return p

    def start(self):
        bus.on("focus", self._on_focus)
        bus.on("wake", lambda *_: threading.Thread(target=self.preload_ollama, daemon=True).start())

    def _on_focus(self, event, data):
        if data.get("mode") == "gaming" and self.provider() == "ollama" and config.get("ai.unload_on_gaming", True):
            threading.Thread(target=self.unload_ollama, daemon=True).start()

    def unload_ollama(self):
        """Geladene Ollama-Modelle sofort aus dem Grafikspeicher werfen (nur wenn wirklich eins geladen ist)."""
        base = (config.get("ai.ollama_url") or "http://localhost:11434").rstrip("/")
        try:
            loaded = [m["name"] for m in requests.get(base + "/api/ps", timeout=3).json().get("models", [])]
        except Exception:
            return
        for name in loaded:
            try:
                requests.post(base + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=10)
            except Exception:
                pass
        if loaded:
            log.activity(M, "Ollama-Modell entladen – Grafikspeicher frei fürs Spiel")

    def client(self):
        key = secrets.get("openai_api_key")
        if not key:
            return None
        if self._client is None or key != self._key:
            from openai import OpenAI
            self._client = OpenAI(api_key=key, timeout=90, max_retries=1)
            self._key = key
        return self._client

    def ollama_client(self):
        from openai import OpenAI
        base = (config.get("ai.ollama_url") or "http://localhost:11434").rstrip("/")
        return OpenAI(base_url=base + "/v1", api_key="ollama", timeout=180, max_retries=0)

    def ollama_running(self):
        t, ok = self._ollama_ok
        if time.time() - t < 20:
            return ok
        try:
            base = (config.get("ai.ollama_url") or "http://localhost:11434").rstrip("/")
            ok = requests.get(base + "/api/tags", timeout=2).ok
        except Exception:
            ok = False
        self._ollama_ok = (time.time(), ok)
        return ok

    def ollama_models(self):
        base = (config.get("ai.ollama_url") or "http://localhost:11434").rstrip("/")
        return [m["name"] for m in requests.get(base + "/api/tags", timeout=4).json().get("models", [])]

    def available(self):
        if not config.get("ai.enabled", True):
            return False
        if self.active_provider() == "ollama":
            return bool(config.get("ai.ollama_model")) and self.ollama_running()
        return not config.get("security.privacy_mode") and secrets.has("openai_api_key")

    def list_models(self):
        if self.provider() == "ollama":
            return self.ollama_models()
        c = self.client()
        if not c:
            return []
        ids = [m.id for m in c.models.list().data]
        chat = [i for i in ids if i.startswith(("gpt-", "o")) and not any(x in i for x in
                ("audio", "tts", "transcribe", "realtime", "image", "embedding", "search", "moderation", "instruct", "codex"))]
        return sorted(chat)

    def test(self):
        if self.provider() == "ollama":
            if not self.ollama_running():
                return False, "Ollama läuft nicht (ollama.com installieren und starten)."
            models = self.ollama_models()
            m = config.get("ai.ollama_model")
            if not m:
                return False, "Ollama läuft, aber es ist kein Modell gewählt." + (f" Verfügbar: {', '.join(models[:5])}" if models else " Lade z. B. mit „ollama pull qwen3“.")
            return (m in models or any(x.startswith(m) for x in models)), f"Ollama bereit, Modell {m}."
        c = self.client()
        if not c:
            return False, "Kein API-Schlüssel hinterlegt."
        try:
            c.models.retrieve(config.get("ai.model"))
            return True, f"Verbunden, Modell {config.get('ai.model')} verfügbar."
        except Exception as e:
            return False, log.friendly(e)

    # -------------------------------------------------------------- Chat
    def _instructions(self, ctx):
        now = datetime.datetime.now().strftime("%A, %d.%m.%Y %H:%M")
        facts = memory.context_text()
        user = config.get("user_name") or ""
        parts = [PERSONA, f"Aktuelle Zeit: {now}."]
        if user:
            parts.append(f"Der Nutzer heißt {user}.")
        if facts:
            parts.append("Bekannte Fakten über den Nutzer (lokales Gedächtnis):\n" + facts)
        try:
            from .pc import tracker, windows
            w = windows.info(tracker.target()) if tracker else None
            if w:
                parts.append(f"Aktives Fenster des Nutzers: „{w['title']}“ ({w['process']}). ‚Das‘ bezieht sich oft darauf.")
        except Exception:
            pass
        if ctx.last_app:
            parts.append(f"Zuletzt gestartetes Programm: {ctx.last_app.get('name')}.")
        if getattr(ctx, "source", "") == "remote":
            parts.append("Der Nutzer spricht gerade über die Handy-App mit dir, nicht direkt am PC.")
        return "\n\n".join(parts)

    def chat(self, text, ctx, silent=False):
        if not self.available():
            if self.active_provider() == "ollama":
                return "Ollama ist nicht erreichbar oder es ist kein Modell gewählt. Bitte in den Einstellungen prüfen."
            return "Für diese Frage brauche ich die KI – bitte hinterlege einen OpenAI-Schlüssel in den Einstellungen."
        try:
            if self.active_provider() == "ollama":
                answer = self._chat_completions(text, ctx)
            else:
                answer = self._chat_openai(text, ctx)
            self.last_error = ""
        except _Early as e:
            self._remember(ctx, text, e.text)
            return e.text
        except Exception as e:
            self.last_error = log.error("KI-Anfrage", e)
            return f"Die KI ist gerade nicht erreichbar: {self.last_error}"
        self._remember(ctx, text, answer)
        return answer or "Erledigt."

    def _chat_openai(self, text, ctx):
        c = self.client()
        tools = registry.ai_tools()
        if config.get("ai.web_search"):
            tools.append({"type": "web_search"})
        hist = ctx.history[-config.get("ai.max_history", 12):]
        inp = [dict(h) for h in hist] + [{"role": "user", "content": text}]
        model = config.get("ai.model")
        for _ in range(6):
            self.requests += 1
            bus.emit("ai_request", model=model)
            resp = c.responses.create(model=model, instructions=self._instructions(ctx), input=inp, tools=tools, store=False)
            costs.track_openai_response(resp, model, "Chat")
            calls = [o for o in resp.output if o.type == "function_call"]
            if not calls:
                return resp.output_text.strip()
            for o in resp.output:
                if o.type == "function_call":
                    inp.append({"type": "function_call", "call_id": o.call_id, "name": o.name, "arguments": o.arguments})
                elif o.type == "message" and getattr(o, "content", None):
                    txt = "".join(getattr(p, "text", "") for p in o.content)
                    if txt:
                        inp.append({"role": "assistant", "content": txt})
            for call in calls:
                out = self._exec_tool(call.name, call.arguments, ctx)
                inp.append({"type": "function_call_output", "call_id": call.call_id, "output": out[:4000]})
            bus.emit("state", state="thinking")
        return ""

    def _chat_completions(self, text, ctx):
        """Ollama (OpenAI-kompatible Chat-Completions-API mit Werkzeugen)."""
        c = self.ollama_client()
        model = config.get("ai.ollama_model")
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["parameters"]}}
                 for t in registry.ai_tools() if t["name"] in OLLAMA_TOOLS]
        hist = ctx.history[-min(6, config.get("ai.max_history", 12)):]   # kurzer Verlauf: Kontext lokaler Modelle ist klein
        msgs = ([{"role": "system", "content": self._instructions(ctx) + "\n\n" + GERMAN_ONLY}]
                + [dict(h) for h in hist] + [{"role": "user", "content": text}])
        for _ in range(6):
            self.requests += 1
            bus.emit("ai_request", model=model)
            resp = self._ollama_create(c, model=model, messages=msgs, tools=tools)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return _strip_think(msg.content or "")
            msgs.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}}
                                        for t in msg.tool_calls]})
            for t in msg.tool_calls:
                out = self._exec_tool(t.function.name, t.function.arguments, ctx)
                msgs.append({"role": "tool", "tool_call_id": t.id, "content": out[:4000]})
            bus.emit("state", state="thinking")
        return ""

    @staticmethod
    def _ollama_create(c, **kw):
        """Ollama-Anfrage: standardmäßig ohne „Nachdenken“ (Qwen3 antwortet so in ~1–3 s statt ~7 s).
        Modelle ohne Nachdenk- oder Werkzeug-Unterstützung werden automatisch ohne diese Optionen erneut gefragt."""
        if not config.get("ai.ollama_think", False):
            kw["reasoning_effort"] = "none"
        for _ in range(3):
            try:
                return c.chat.completions.create(**kw)
            except Exception as e:
                err = str(e).lower()
                if "reasoning_effort" in kw and ("think" in err or "reasoning" in err):
                    kw.pop("reasoning_effort")
                elif kw.get("tools") and "does not support tools" in err:
                    kw.pop("tools")
                else:
                    raise
        return c.chat.completions.create(**kw)

    def preload_ollama(self):
        """Modell im Hintergrund in den Grafikspeicher laden (z. B. sobald „Hey Jarvis“ erkannt wurde),
        damit die Antwort ohne die ~5 s Ladezeit kommt."""
        if self.active_provider() != "ollama" or not config.get("ai.ollama_model"):
            return
        base = (config.get("ai.ollama_url") or "http://localhost:11434").rstrip("/")
        try:
            requests.post(base + "/api/generate", json={"model": config.get("ai.ollama_model"), "prompt": "", "keep_alive": "10m"}, timeout=60)
        except Exception:
            pass

    def _exec_tool(self, name, arguments, ctx):
        """Führt ein von der KI gewünschtes Werkzeug aus – mit derselben Sicherheitsprüfung wie lokal."""
        try:
            args = json.loads(arguments or "{}") if isinstance(arguments, str) else dict(arguments or {})
        except Exception:
            args = {}
        act = registry.get(name)
        if act and act.risk >= int(config.get("security.confirm_level", 2)):
            raise _Early(self.jarvis.brain.execute([(name, args)]))
        bus.emit("state", state="executing")
        res = None
        try:
            res = registry.call(name, args, ctx)
            out = res.text or ("ok" if res.ok else "fehlgeschlagen")
            if isinstance(res.data, dict) and res.data.get("choose"):
                ctx.pending = {"type": "choose", "action": name, "args": args,
                               "param": res.data["param"], "options": res.data["choose"], "time": time.time()}
                raise _Early(res.text)
        except _Early:
            raise
        except Exception as e:
            out = "Fehler: " + log.error(f"KI-Aktion {name}", e)
        log.activity(act.module if act else M, f"(KI) {act.description if act else name} → {out}", bool(res and res.ok))
        if res and res.ok and act and act.risk > 0:
            ctx.last_calls = [(name, args)]
        return out

    def complete(self, prompt, system="", web=False, timeout=None):
        """Einzelne Anfrage ohne Werkzeuge (für Briefing, Recherche …)."""
        system = system or PERSONA
        self.requests += 1
        if self.active_provider() == "ollama":
            resp = self._ollama_create(self.ollama_client(), model=config.get("ai.ollama_model"),
                                       messages=[{"role": "system", "content": system + "\n\n" + GERMAN_ONLY}, {"role": "user", "content": prompt}])
            return _strip_think(resp.choices[0].message.content or "")
        c = self.client()
        kw = {"tools": [{"type": "web_search"}]} if web else {}
        resp = c.responses.create(model=config.get("ai.model"), instructions=system, input=prompt, store=False, **kw)
        costs.track_openai_response(resp, config.get("ai.model"), "Briefing & Recherche")
        return resp.output_text.strip()

    @staticmethod
    def _remember(ctx, user, answer):
        ctx.history += [{"role": "user", "content": user}, {"role": "assistant", "content": answer or ""}]
        ctx.history = ctx.history[-40:]

    def vision(self, question, image_bytes):
        c = self.client()
        if not c:
            return "Für die Bildschirmanalyse brauche ich einen OpenAI-Schlüssel."
        b64 = base64.b64encode(image_bytes).decode()
        self.requests += 1
        resp = c.responses.create(
            model=config.get("ai.vision_model") or config.get("ai.model"),
            instructions=PERSONA + "\nDu siehst einen Screenshot des Bildschirms des Nutzers.",
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": question or "Was ist auf meinem Bildschirm zu sehen?"},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}]}],
            store=False)
        costs.track_openai_response(resp, config.get("ai.vision_model") or config.get("ai.model"), "Bildschirmanalyse")
        return resp.output_text.strip()

    # ------------------------------------------------------------- Modul
    def parse(self, text, n, ctx):
        if re.search(r"(was siehst du|analysier(e)? (den |meinen )?bildschirm|was ist auf (meinem |dem )?bildschirm|schau dir (den |meinen )?bildschirm an|was steht (da|hier|auf dem bildschirm)|lies (mir )?vor,? was (da|hier) steht|erklär(e)? mir (das|was ich sehe))", n):
            return Intent([("analyze_screen", {"question": text})])
        return None

    def status(self):
        prov = self.provider()
        return {"available": self.available(), "has_key": secrets.has("openai_api_key"), "provider": prov,
                "active": self.active_provider(),
                "model": config.get("ai.ollama_model") if prov == "ollama" else config.get("ai.model"),
                "requests": self.requests, "web_search": config.get("ai.web_search"), "last_error": self.last_error}

    def diagnose(self):
        if config.get("security.privacy_mode") and self.provider() != "ollama":
            return [("KI", True, "Datenschutzmodus aktiv – OpenAI deaktiviert")]
        ok, msg = self.test()
        return [("KI-Verbindung (" + self.provider() + ")", ok, msg)]


class _Early(Exception):
    """Die KI-Schleife endet vorzeitig (Rückfrage/Bestätigung nötig)."""
    def __init__(self, text):
        super().__init__(text)
        self.text = text


def _strip_think(s):
    # Reasoning-Modelle (z. B. qwen3/deepseek-r1) liefern <think>…</think>
    return re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()


@action("analyze_screen", "Analysiert den aktuellen Bildschirminhalt mit der KI und beantwortet eine Frage dazu",
        {"question": S("Frage zum Bildschirm")}, risk=0, module=M, ai=False)
def analyze_screen(question="", ctx=None):
    from .pc import system
    if config.get("security.privacy_mode") or not config.get("security.screen_ai", True):
        return {"ok": False, "text": "Bildschirmanalyse ist im Datenschutzmodus deaktiviert."}
    ai = _instance
    if not ai or not secrets.has("openai_api_key"):
        return {"ok": False, "text": "Für die Bildschirmanalyse brauche ich einen OpenAI-Schlüssel."}
    img = system.screenshot_bytes()
    return ai.vision(question, img)
