"""Sprache: Wake-Word, Aufnahme, Spracherkennung (STT) und Sprachausgabe (TTS).

TTS-Engines:
    edge    – natürliche Microsoft-Neural-Stimmen (online, kostenlos)
    openai  – hochwertige OpenAI-KI-Stimme (kostenpflichtig, pro Zeichen)
    elevenlabs – sehr natürliche Premium-Stimmen von ElevenLabs (eigener API-Schlüssel)
    system  – lokale Windows-Stimme (offline)
STT-Engines:
    openai  – OpenAI-Transkription (sehr genau)
    local   – faster-whisper, läuft komplett lokal
"""
import asyncio
import io
import queue
import re
import threading
import time
import wave

import numpy as np

from ..core import log, paths, secrets
from ..core.actions import action, S
from ..core.config import config
from ..core.events import bus
from ..core.module import Module
from . import costs

M = "sprache"
RATE = 16000
BLOCK = 1280  # 80 ms – Blockgröße für openWakeWord

EDGE_VOICES = [
    ("de-DE-ConradNeural", "Conrad (männlich, ruhig)"),
    ("de-DE-FlorianMultilingualNeural", "Florian (männlich, sehr natürlich)"),
    ("de-DE-KillianNeural", "Killian (männlich)"),
    ("de-AT-JonasNeural", "Jonas (männlich, österreichisch)"),
    ("de-CH-JanNeural", "Jan (männlich, schweizerisch)"),
    ("de-DE-SeraphinaMultilingualNeural", "Seraphina (weiblich, sehr natürlich)"),
    ("de-DE-KatjaNeural", "Katja (weiblich)"),
    ("de-DE-AmalaNeural", "Amala (weiblich)"),
]
OPENAI_VOICES = ["onyx", "ash", "echo", "fable", "sage", "verse", "cedar", "marin", "alloy", "ballad", "coral", "nova", "shimmer"]
OPENAI_STYLE = ("Sprich Deutsch als JARVIS: ein ruhiger, eleganter, leicht britisch-trockener KI-Butler. "
                "Klar, souverän, freundlich, nicht übertrieben.")
ELEVEN_API = "https://api.elevenlabs.io/v1"
# Fertige ElevenLabs-Stimmen (alle sprechen mit dem Multilingual-Modell Deutsch)
ELEVEN_DEFAULT_VOICES = [
    ("onwK4e9ZLuTAKqWW03F9", "Daniel (männlich, britisch, ruhig)"),
    ("JBFqnCBsd6RMkjVDRZzb", "George (männlich, warm)"),
    ("nPczCjzI2devNBz1zQrb", "Brian (männlich, tief)"),
    ("pNInz6obpgDQGcFmaJgB", "Adam (männlich, kräftig)"),
    ("EXAVITQu4vr4xnSDxMaL", "Sarah (weiblich, sanft)"),
    ("21m00Tcm4TlvDq8ikWAM", "Rachel (weiblich, ruhig)"),
]
ONLINE_TTS = ("edge", "openai", "elevenlabs")

_instance = None


def _dev_index(name, kind):
    if name in (None, "", "default"):
        return None
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and d[f"max_{kind}_channels"] > 0:
            return i
    return None


# =================================================================== Ausgabe
class Speaker:
    """Satzweise Synthese + unterbrechbare Wiedergabe mit Pegel für die Animation."""

    def __init__(self, voice):
        self.voice = voice
        self.q = queue.Queue()
        self.stop_flag = threading.Event()
        self.speaking = False
        self.gen = 0
        threading.Thread(target=self._loop, daemon=True, name="tts").start()

    def say(self, text):
        text = _speakable(text)
        if not text:
            return
        self.q.put((self.gen, text))

    def stop(self):
        self.gen += 1
        self.stop_flag.set()
        with self.q.mutex:
            self.q.queue.clear()
        try:
            if getattr(self, "_sapi", None):
                self._sapi.Speak("", 3)  # purge
        except Exception:
            pass

    def _loop(self):
        while True:
            gen, text = self.q.get()
            if gen != self.gen:
                continue
            self.stop_flag.clear()
            self.speaking = True
            bus.emit("state", state="speaking")
            try:
                engine = config.get("voice.tts_engine")
                if config.get("security.privacy_mode") and engine in ONLINE_TTS:
                    engine = "system"
                if engine == "system":
                    self._sapi_say(text)
                else:
                    self._neural(text, engine, gen)
            except Exception as e:
                log.error("Sprachausgabe", e)
                try:
                    self._sapi_say(text)  # Rückfallebene
                except Exception:
                    pass
            finally:
                if self.q.empty():
                    self.speaking = False
                    bus.emit("level", level=0)
                    bus.emit("speech_done")
                    bus.emit("state", state="idle")

    def _neural(self, text, engine, gen):
        sentences = _split_sentences(text)
        audio_q = queue.Queue(maxsize=3)

        def synth():
            for s in sentences:
                if gen != self.gen or self.stop_flag.is_set():
                    break
                try:
                    synth_fn = {"openai": self._synth_openai, "elevenlabs": self._synth_elevenlabs}.get(engine, self._synth_edge)
                    pcm, sr = synth_fn(s)
                    audio_q.put((pcm, sr))
                except Exception as e:
                    msg = log.error("Stimme", e)
                    now = time.time()
                    if now - getattr(self, "_last_err_notify", 0) > 60:   # nicht bei jedem Satz melden
                        self._last_err_notify = now
                        bus.emit("notify", title="Stimme", text=msg + " Ich spreche vorerst mit der Windows-Stimme.", speak=False)
                    audio_q.put(("error", s))
                    break
            audio_q.put(None)

        threading.Thread(target=synth, daemon=True).start()
        while True:
            item = audio_q.get()
            if item is None or self.stop_flag.is_set():
                break
            if isinstance(item[0], str) and item[0] == "error":   # item[0] ist sonst ein numpy-Array
                self._sapi_say(item[1])
                break
            self._play(*item)

    def _synth_edge(self, text):
        import edge_tts
        import miniaudio
        rate = int(config.get("voice.rate", 0))
        comm = edge_tts.Communicate(text, config.get("voice.edge_voice"), rate=f"{rate:+d}%")

        async def collect():
            buf = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        mp3 = asyncio.run(collect())
        dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=24000)
        return np.frombuffer(dec.samples, dtype=np.int16), 24000

    def _synth_openai(self, text):
        from openai import OpenAI
        client = OpenAI(api_key=secrets.get("openai_api_key"), timeout=30)
        rate = int(config.get("voice.rate", 0))
        style = OPENAI_STYLE + (" Sprich etwas schneller." if rate > 10 else " Sprich etwas langsamer." if rate < -10 else "")
        resp = client.audio.speech.create(model=config.get("voice.openai_tts_model"), voice=config.get("voice.openai_voice"),
                                          input=text, instructions=style, response_format="pcm")
        pcm = np.frombuffer(resp.content, dtype=np.int16)
        costs.track_openai_tts(len(pcm) / 24000, config.get("voice.openai_tts_model"))
        return pcm, 24000

    def _synth_elevenlabs(self, text):
        import requests
        key = secrets.get("elevenlabs_api_key")
        if not key:
            raise RuntimeError("Kein ElevenLabs-Schlüssel eingerichtet.")
        rate = int(config.get("voice.rate", 0))
        r = requests.post(
            f"{ELEVEN_API}/text-to-speech/{config.get('voice.elevenlabs_voice')}",
            params={"output_format": "pcm_24000"},
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": text, "model_id": config.get("voice.elevenlabs_model"), "language_code": "de",
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                     "speed": max(0.7, min(1.2, 1 + rate / 100))}},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(_elevenlabs_error(r))
        costs.track_elevenlabs(len(text), config.get("voice.elevenlabs_model"))
        return np.frombuffer(r.content, dtype=np.int16), 24000

    def _play(self, pcm, sr):
        import sounddevice as sd
        vol = max(0, min(100, int(config.get("voice.volume", 90)))) / 100
        data = (pcm.astype(np.float32) / 32768.0) * vol
        pos = [0]
        done = threading.Event()
        last = [0.0]

        def cb(out, frames, t, status):
            if self.stop_flag.is_set():
                out[:] = 0
                raise sd.CallbackStop
            chunk = data[pos[0]:pos[0] + frames]
            out[:len(chunk), 0] = chunk
            out[len(chunk):, 0] = 0
            pos[0] += frames
            now = time.time()
            if now - last[0] > 0.05 and len(chunk):
                last[0] = now
                bus.emit("level", level=float(min(1.0, np.sqrt(np.mean(chunk ** 2)) * 4)))
            if pos[0] >= len(data):
                raise sd.CallbackStop

        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", callback=cb,
                             device=_dev_index(config.get("voice.output_device"), "output"),
                             finished_callback=done.set):
            while not done.wait(0.05):
                if self.stop_flag.is_set():
                    break

    def _sapi_say(self, text):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        self._sapi = sp
        try:
            for v in sp.GetVoices():
                if "German" in v.GetDescription() or "Deutsch" in v.GetDescription():
                    sp.Voice = v
                    break
        except Exception:
            pass
        sp.Volume = int(config.get("voice.volume", 90))
        sp.Rate = max(-10, min(10, int(config.get("voice.rate", 0)) // 5))
        sp.Speak(text, 1)  # asynchron
        t = 0
        while not sp.WaitUntilDone(100):
            if self.stop_flag.is_set():
                sp.Speak("", 3)
                break
            t += 1
            bus.emit("level", level=0.3 + 0.3 * abs(np.sin(t / 2)))
        self._sapi = None


def _elevenlabs_error(r) -> str:
    """Verständliche Meldung aus einer ElevenLabs-Fehlerantwort."""
    try:
        detail = r.json().get("detail") or {}
    except Exception:
        detail = {}
    if isinstance(detail, str):
        detail = {"message": detail}
    code = f"{detail.get('code', '')} {detail.get('status', '')} {detail.get('type', '')}".lower()
    if "paid_plan_required" in code or "library voice" in (detail.get("message") or "").lower():
        return ("Diese ElevenLabs-Stimme stammt aus der Voice Library und braucht ein Abo. "
                "Wähle eine Standardstimme wie Daniel, George oder Brian.")
    if "quota" in code:
        return "Dein ElevenLabs-Guthaben für diesen Monat ist aufgebraucht."
    if "missing_permissions" in code:
        return "Dem ElevenLabs-Schlüssel fehlt die Berechtigung „Text zu Sprache“."
    if "unusual_activity" in code:
        return "ElevenLabs hat den kostenlosen Zugang vorübergehend gesperrt (ungewöhnliche Aktivität)."
    if r.status_code == 401:
        return "ElevenLabs-Schlüssel ungültig."
    if r.status_code == 404:
        return "Diese ElevenLabs-Stimme gibt es nicht mehr – bitte eine andere wählen."
    return f"ElevenLabs-Fehler {r.status_code}: {detail.get('message') or r.reason}"


def _speakable(text):
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"[*_#`>|]+", "", t)
    t = t.replace("→", ",").replace("–", ",")
    return re.sub(r"\s+", " ", t).strip()


def _split_sentences(text):
    parts = re.split(r"(?<=[.!?…])\s+", text)
    out, buf = [], ""
    for p in parts:
        buf = (buf + " " + p).strip()
        if len(buf) > 40 or p is parts[-1]:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def chime(kind="listen"):
    if not config.get("voice.chime", True):
        return
    try:
        import sounddevice as sd
        sr = 24000
        freqs = (880, 1320) if kind == "listen" else (1320, 880)
        tone = np.concatenate([np.sin(2 * np.pi * f * np.arange(int(sr * 0.07)) / sr) for f in freqs])
        env = np.minimum(1, np.minimum(np.arange(len(tone)), np.arange(len(tone))[::-1]) / 300)
        sd.play((tone * env * 0.15 * config.get("voice.volume", 90) / 100).astype(np.float32), sr,
                device=_dev_index(config.get("voice.output_device"), "output"))
    except Exception:
        pass


# =================================================================== Eingabe
class Listener:
    """Dauerhafter Mikrofon-Stream: Wake-Word-Erkennung und Aufnahme."""

    def __init__(self, voice):
        self.voice = voice
        self.frames = queue.Queue(maxsize=200)
        self.stream = None
        self.mode = "off"          # off | wake | record
        self.oww = None
        self.rec = []
        self.rec_start = 0
        self.last_voice = 0
        self.heard = False
        self.noise = 300.0
        self.follow_up_until = 0
        self.error = ""
        self.last_level = 0
        threading.Thread(target=self._consume, daemon=True, name="listener").start()

    def ensure_stream(self):
        if self.stream:
            return True
        import sounddevice as sd
        try:
            self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                         device=_dev_index(config.get("voice.input_device"), "input"),
                                         callback=self._cb)
            self.stream.start()
            self.error = ""
            return True
        except Exception as e:
            self.stream = None
            self.error = log.error("Mikrofon", e)
            bus.emit("notify", title="Mikrofon", text="Mikrofon konnte nicht geöffnet werden: " + self.error, speak=False)
            return False

    def close_stream(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _cb(self, indata, frames, t, status):
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def load_wake(self):
        if self.oww:
            return True
        try:
            import os
            import sys
            import types
            # Trainingsmodul (braucht scikit-learn/SciPy) wird zur Laufzeit nicht benötigt
            if "openwakeword.custom_verifier_model" not in sys.modules:
                stub = types.ModuleType("openwakeword.custom_verifier_model")
                stub.train_custom_verifier = None
                sys.modules["openwakeword.custom_verifier_model"] = stub
            from openwakeword.model import Model
            import openwakeword
            base = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
            self.oww = Model(wakeword_models=[os.path.join(base, "hey_jarvis_v0.1.onnx")],
                             melspec_model_path=os.path.join(base, "melspectrogram.onnx"),
                             embedding_model_path=os.path.join(base, "embedding_model.onnx"),
                             inference_framework="onnx")
            return True
        except Exception as e:
            self.error = log.error("Wake-Word-Modell", e)
            return False

    def set_wake(self, on):
        if on:
            if self.load_wake() and self.ensure_stream():
                if self.mode == "off":
                    self.mode = "wake"
        else:
            if self.mode == "wake":
                self.mode = "off"
            if self.mode == "off":
                self.close_stream()

    def start_record(self):
        if not self.ensure_stream():
            return False
        with self.frames.mutex:
            self.frames.queue.clear()
        self.rec, self.heard = [], False
        self.rec_start = self.last_voice = time.time()
        self.mode = "record"
        bus.emit("state", state="listening")
        chime("listen")
        return True

    def cancel_record(self):
        if self.mode == "record":
            self.mode = "wake" if config.get("voice.wake_word") and self.oww else "off"
            bus.emit("state", state="idle")
            if self.mode == "off":
                self.close_stream()

    def _consume(self):
        while True:
            try:
                frame = self.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            level = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            now = time.time()
            if self.mode == "record":
                self._record(frame, level, now)
            elif self.mode == "wake" and self.oww:
                # Grundrauschen nachführen
                self.noise = 0.98 * self.noise + 0.02 * level
                try:
                    score = max(self.oww.predict(frame).values(), default=0)
                except Exception:
                    score = 0
                if score >= float(config.get("voice.wake_threshold", 0.5)):
                    self.oww.reset()
                    log.logger().info("Wake-Word erkannt (%.2f)", score)
                    self.voice.on_wake()

    def _record(self, frame, level, now):
        self.rec.append(frame)
        if now - self.last_level > 0.05:
            self.last_level = now
            bus.emit("level", level=min(1.0, level / 3000))
        threshold = max(500, self.noise * 2.5)
        if level > threshold:
            self.heard = True
            self.last_voice = now
        silence = now - self.last_voice
        total = now - self.rec_start
        max_silence = float(config.get("voice.silence_seconds", 1.8))
        max_total = float(config.get("voice.max_record_seconds", 30))
        if (self.heard and silence > max_silence) or total > max_total or (not self.heard and total > 8):
            audio = np.concatenate(self.rec) if self.rec else np.zeros(0, np.int16)
            heard = self.heard
            self.rec = []
            self.mode = "wake" if config.get("voice.wake_word") and self.oww else "off"
            if self.mode == "off":
                threading.Thread(target=self.close_stream, daemon=True).start()
            if heard and len(audio) > RATE * 0.4:
                threading.Thread(target=self.voice.transcribe_and_submit, args=(audio,), daemon=True).start()
            else:
                bus.emit("state", state="idle")


# ================================================================== Modul
class VoiceModule(Module):
    name = "voice"
    title = "Sprache"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _instance
        _instance = self
        self.speaker = Speaker(self)
        self.listener = Listener(self)
        self._whisper = None
        self._whisper_loading = False
        self.last_transcript = ""
        self.awaiting_follow_up = False
        bus.on("speech_done", self._after_speech)

    def start(self):
        if config.get("voice.wake_word"):
            threading.Thread(target=lambda: self.listener.set_wake(True), daemon=True).start()
        config.on_change(self._cfg)

    def stop(self):
        self.speaker.stop()
        self.listener.close_stream()

    def _cfg(self, key, value):
        if key == "voice.wake_word":
            threading.Thread(target=lambda: self.listener.set_wake(bool(value)), daemon=True).start()
        if key in ("voice.input_device",):
            self.listener.close_stream()
            if config.get("voice.wake_word"):
                self.listener.set_wake(True)

    def parse(self, text, n, ctx):
        return parse_voice(n)

    # ------------------------------------------------------------- TTS
    def speak(self, text):
        if not config.get("voice.tts_enabled") or config.get("voice.tts_engine") == "off":
            return
        self.speaker.say(text)

    def is_speaking(self):
        return self.speaker.speaking

    def stop_speaking(self):
        self.speaker.stop()

    # ------------------------------------------------------------- STT
    def on_wake(self):
        # Unterbrechen, falls JARVIS gerade spricht
        if self.speaker.speaking:
            self.speaker.stop()
        bus.emit("wake")
        self.listener.start_record()

    def toggle_listen(self):
        if self.listener.mode == "record":
            self.listener.cancel_record()
            return False
        self.speaker.stop()
        return self.listener.start_record()

    def _after_speech(self, *_):
        # Folgefrage ohne erneutes Wake-Word
        if self.awaiting_follow_up and config.get("voice.always_listen"):
            self.awaiting_follow_up = False
            time.sleep(0.25)
            if self.listener.mode != "record" and not self.jarvis.brain.ctx.cancel.is_set():
                self.listener.start_record()

    def transcribe_and_submit(self, audio):
        bus.emit("state", state="thinking")
        try:
            text = self.transcribe(audio)
        except Exception as e:
            msg = log.error("Spracherkennung", e)
            bus.emit("state", state="idle")
            self.jarvis.brain.reply("Ich konnte dich nicht verstehen: " + msg, speak=False)
            return
        text = (text or "").strip()
        # typische Whisper-Halluzinationen bei Stille verwerfen
        if not text or re.fullmatch(r"(?i)[\s.,!?…]*|untertitel.*|vielen dank\.?|tschüss\.?|copyright.*", text):
            bus.emit("state", state="idle")
            return
        self.last_transcript = text
        bus.emit("transcript", text=text)
        self.awaiting_follow_up = True
        self.jarvis.brain.submit(text, source="voice")

    def transcribe(self, audio: np.ndarray) -> str:
        engine = config.get("voice.stt_engine")
        if config.get("security.privacy_mode") or not secrets.has("openai_api_key"):
            engine = "local"
        if engine == "openai":
            from openai import OpenAI
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(audio.astype(np.int16).tobytes())
            client = OpenAI(api_key=secrets.get("openai_api_key"), timeout=30)
            r = client.audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=("audio.wav", buf.getvalue()),
                                                   language="de", prompt="JARVIS, Sprachbefehle am PC.")
            costs.track_transcription(len(audio) / RATE)
            return r.text
        model = self.whisper()
        segs, _ = model.transcribe(audio.astype(np.float32) / 32768.0, language="de", beam_size=1, vad_filter=True,
                                   initial_prompt="JARVIS, starte Discord. Mach das leiser.")
        return " ".join(s.text for s in segs)

    def transcribe_bytes(self, data: bytes, filename="audio.webm") -> str:
        """Transkribiert eine Audiodatei (z. B. Aufnahme aus der Handy-App: webm/ogg/mp4)."""
        use_openai = (config.get("voice.stt_engine") == "openai" and secrets.has("openai_api_key")
                      and not config.get("security.privacy_mode"))
        if use_openai:
            from openai import OpenAI
            client = OpenAI(api_key=secrets.get("openai_api_key"), timeout=30)
            r = client.audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=(filename, data),
                                                   language="de", prompt="JARVIS, Sprachbefehle.")
            usage = getattr(r, "usage", None)
            seconds = getattr(usage, "seconds", None) if usage else None
            costs.track_transcription(seconds or len(data) / 4000)   # Handy-Aufnahme (Opus) ≈ 4 kB pro Sekunde
            return r.text
        from faster_whisper.audio import decode_audio
        audio = decode_audio(io.BytesIO(data), sampling_rate=RATE)
        segs, _ = self.whisper().transcribe(audio, language="de", beam_size=1, vad_filter=True)
        return " ".join(s.text for s in segs)

    def whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            bus.emit("notify", title="Lokale Spracherkennung", text="Lade Sprachmodell (beim ersten Mal wird es heruntergeladen) …", speak=False)
            self._whisper = WhisperModel(config.get("voice.local_stt_model", "small"), device="cpu", compute_type="int8",
                                         download_root=str(paths.MODELS_DIR / "whisper"))
        return self._whisper

    # ------------------------------------------------------------ Status
    def status(self):
        return {"mode": self.listener.mode, "speaking": self.speaker.speaking, "wake": bool(config.get("voice.wake_word")),
                "error": self.listener.error, "tts": config.get("voice.tts_engine"), "stt": config.get("voice.stt_engine")}

    def diagnose(self):
        out = []
        import sounddevice as sd
        try:
            dev = sd.query_devices(_dev_index(config.get("voice.input_device"), "input") or sd.default.device[0])
            rec = sd.rec(int(RATE * 1.0), samplerate=RATE, channels=1, dtype="int16",
                         device=_dev_index(config.get("voice.input_device"), "input")) if not self.listener.stream else None
            if rec is not None:
                sd.wait()
                lvl = float(np.sqrt(np.mean(rec.astype(np.float32) ** 2)))
                out.append(("Mikrofon", lvl > 5, f"{dev['name']} – Pegel {lvl:.0f}" + ("" if lvl > 5 else " (sehr leise/stumm?)")))
            else:
                out.append(("Mikrofon", True, f"{dev['name']} – aktiv (Wake-Word läuft)"))
        except Exception as e:
            out.append(("Mikrofon", False, log.friendly(e)))
        try:
            sd.query_devices(_dev_index(config.get("voice.output_device"), "output") or sd.default.device[1])
            engine = config.get("voice.tts_engine")
            if engine == "edge":
                self.speaker._synth_edge("Test.")
            elif engine == "openai":
                self.speaker._synth_openai("Test.")
            elif engine == "elevenlabs":
                self.speaker._synth_elevenlabs("Test.")
            out.append(("Stimme", True, f"Engine „{engine}“ funktioniert"))
        except Exception as e:
            out.append(("Stimme", False, log.friendly(e)))
        out.append(("Wake-Word", bool(self.listener.oww) or not config.get("voice.wake_word"),
                    "aktiv" if self.listener.oww else ("aus" if not config.get("voice.wake_word") else self.listener.error or "nicht geladen")))
        return out


def list_voices():
    sapi = []
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        sapi = [v.GetDescription() for v in win32com.client.Dispatch("SAPI.SpVoice").GetVoices()]
    except Exception:
        pass
    return {"edge": EDGE_VOICES, "openai": OPENAI_VOICES, "elevenlabs": elevenlabs_voices()[1], "system": sapi}


def elevenlabs_voices():
    """(ok, [(voice_id, name)], Meldung) – eigene und fertige Stimmen des ElevenLabs-Kontos."""
    key = secrets.get("elevenlabs_api_key")
    if not key:
        return False, ELEVEN_DEFAULT_VOICES, "Kein Schlüssel eingerichtet."
    try:
        import requests
        r = requests.get(f"{ELEVEN_API}/voices", headers={"xi-api-key": key}, timeout=10)
        if r.status_code == 401:
            return False, ELEVEN_DEFAULT_VOICES, "Schlüssel ungültig oder ohne Berechtigung „Voices: Read“."
        r.raise_for_status()
        voices = []
        for v in r.json().get("voices", []):
            labels = v.get("labels") or {}
            extra = ", ".join(x for x in (labels.get("gender"), labels.get("accent")) if x)
            # Voice-Library-Stimmen („professional“) gehen über die API nur mit Bezahl-Abo
            paid = v.get("category") == "professional"
            name = v["name"] + (f" ({extra})" if extra else "") + (" – nur mit Abo" if paid else "")
            voices.append((paid, v["voice_id"], name))
        voices.sort(key=lambda x: (x[0], x[2].lower()))
        voices = [(vid, name) for _, vid, name in voices]
        return True, voices or ELEVEN_DEFAULT_VOICES, f"Verbunden – {len(voices)} Stimmen verfügbar."
    except Exception as e:
        return False, ELEVEN_DEFAULT_VOICES, log.friendly(e)


def audio_devices():
    import sounddevice as sd
    ins, outs = [], []
    hostapis = sd.query_hostapis()
    for d in sd.query_devices():
        api = hostapis[d["hostapi"]]["name"]
        if "MME" not in api or "Soundmapper" in d["name"]:  # MME listet jedes Gerät genau einmal
            continue
        if d["max_input_channels"] > 0:
            ins.append(d["name"])
        if d["max_output_channels"] > 0:
            outs.append(d["name"])
    return {"inputs": ins, "outputs": outs}


# ------------------------------------------------------------------ Aktionen
@action("say", "Spricht einen Text laut aus", {"text": S("Text")}, risk=0, module=M, ai=False)
def say(text):
    if _instance:
        _instance.speak(text)
    return {"ok": True, "text": ""}


@action("voice_settings", "Ändert Stimme, Lautstärke oder Sprechtempo von JARVIS",
        {"setting": S("volume, rate, voice, tts_on, tts_off", ["volume", "rate", "voice", "tts_on", "tts_off"]),
         "value": S("Wert: Zahl für volume (0-100)/rate (-50..50), Name für voice")}, risk=0, module=M)
def voice_settings(setting, value=""):
    if setting == "volume":
        config.set("voice.volume", int(float(value)))
        return f"Meine Lautstärke ist jetzt {int(float(value))} Prozent."
    if setting == "rate":
        config.set("voice.rate", max(-50, min(50, int(float(value)))))
        return "Sprechtempo angepasst."
    if setting == "tts_off":
        config.set("voice.tts_enabled", False)
        return "Sprachausgabe ist aus. Ich antworte nur noch schriftlich."
    if setting == "tts_on":
        config.set("voice.tts_enabled", True)
        return "Sprachausgabe ist wieder an."
    if setting == "voice":
        from ..core.text import similarity
        best = max(EDGE_VOICES, key=lambda v: similarity(value, v[1].split(" (")[0]))
        if similarity(value, best[1].split(" (")[0]) > 0.6:
            config.set("voice.tts_engine", "edge")
            config.set("voice.edge_voice", best[0])
            return f"Ich spreche jetzt mit der Stimme {best[1].split(' (')[0]}."
        if value.lower() in OPENAI_VOICES:
            config.set("voice.tts_engine", "openai")
            config.set("voice.openai_voice", value.lower())
            return f"Ich nutze jetzt die OpenAI-Stimme {value}."
        return {"ok": False, "text": f"Eine Stimme „{value}“ kenne ich nicht."}
    return {"ok": False, "text": "Unbekannte Einstellung."}


def parse_voice(n):
    """Sprach-Einstellungen per Sprache („sprich schneller“, „sei leiser“)."""
    from ..core.module import Intent
    from ..core.text import find_number
    if re.search(r"(sprich|rede) (etwas |ein bisschen |viel )?schneller", n):
        return Intent([("voice_settings", {"setting": "rate", "value": str(min(50, config.get("voice.rate", 0) + 15))})])
    if re.search(r"(sprich|rede) (etwas |ein bisschen |viel )?langsamer", n):
        return Intent([("voice_settings", {"setting": "rate", "value": str(max(-50, config.get("voice.rate", 0) - 15))})])
    if re.search(r"(sprich|rede) (etwas |ein bisschen )?(leiser)|deine stimme leiser|sei leiser", n):
        return Intent([("voice_settings", {"setting": "volume", "value": str(max(10, config.get("voice.volume", 90) - 20))})])
    if re.search(r"(sprich|rede) (etwas |ein bisschen )?(lauter)|deine stimme lauter|sei lauter", n):
        return Intent([("voice_settings", {"setting": "volume", "value": str(min(100, config.get("voice.volume", 90) + 20))})])
    if re.search(r"(sprachausgabe|stimme|sprechen) (aus|deaktivieren|abschalten)|hör auf zu sprechen|antworte nur (noch )?schriftlich", n):
        return Intent([("voice_settings", {"setting": "tts_off"})])
    if re.search(r"(sprachausgabe|stimme) (an|aktivieren|einschalten)|sprich wieder( mit mir)?", n):
        return Intent([("voice_settings", {"setting": "tts_on"})])
    m = re.search(r"(nimm|nutze|verwende|wechsle zu|wechsel zu|sprich mit) (der |die )?stimme (.+)$", n)
    if m:
        return Intent([("voice_settings", {"setting": "voice", "value": m.group(3)})])
    return None
