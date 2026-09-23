# JARVIS – persönlicher Assistent für Windows

## Installieren
`dist\JARVIS-Setup-1.0.0.exe` ausführen. Installiert pro Benutzer (ohne Admin-Rechte) nach
`%LOCALAPPDATA%\Programs\JARVIS`, legt Desktop-Verknüpfung und Startmenü-Eintrag an.
Beim ersten Start führt der Einrichtungsassistent durch API-Schlüssel, Stimme, Mikrofon, Programme, Handy und Zimmer.

Persönliche Daten liegen getrennt und bleiben bei Updates/Neuinstallation erhalten:
- `%APPDATA%\JARVIS` – Gedächtnis, Profile, Routinen, Geräte, Einstellungen, Backups, Protokolle
- `%LOCALAPPDATA%\JARVIS` – Caches, lokale Sprachmodelle, ADB, Update-Rollback
- API-Schlüssel/Tokens – Windows-Anmeldeinformationsverwaltung (nicht im Klartext)

## Beispiele
| Sagen / schreiben | Ergebnis |
|---|---|
| „Hey Jarvis, starte Discord“ | Programm starten (lokal, ohne KI) |
| „Schließ das“ · „Mach das leiser“ · „Mach das nochmal“ | Kontextbezogene Befehle |
| „Mein Lieblingsspiel ist GTA“ → „Was ist mein Lieblingsspiel?“ | Dauerhaftes Gedächtnis |
| „Wenn ich FiveM sage, starte Discord, TeamSpeak und FiveM mit 5 Sekunden Pause“ | Eigener Befehl |
| „Erstelle ein Profil GTA mit Steam und Discord“ | Spielprofil |
| „Jeden Tag um 23 Uhr sperre den PC“ | Zeitgesteuerte Routine |
| „Füge im Wohnzimmer eine Lampe namens Deckenlicht hinzu“ → „Licht im Wohnzimmer aus“ | Smart Home |
| „Was siehst du auf meinem Bildschirm?“ | Bildschirmanalyse (KI) |
| „Jarvis, stopp“ / Not-Aus-Knopf | Stoppt Sprache und Automationen |
| „Guten Morgen“ / „Briefing“ | Morgen-Briefing: Datum, Wetter, heutige Routinen, Notizen, PC-Hinweise, Nachrichten |
| „Wie wird das Wetter morgen in Hamburg?“ | Wetter (Open-Meteo, ohne Schlüssel) |
| „Recherchiere über …“ | Recherche-Agent: Teilfragen + Websuche → Bericht in Dokumente\JARVIS\Recherchen |

## Handy-App (wie Alexa)
JARVIS → Handy → „JARVIS-Handy-App“ aktivieren, QR-Code mit dem Handy scannen (gleiches WLAN), Zertifikatswarnung
einmal bestätigen, „Zum Startbildschirm hinzufügen“. Sprach- und Texteingabe, Status, Geräte, Routinen, Not-Aus.
HTTPS mit selbst erzeugtem Zertifikat, Zugriff nur mit geheimem Token (steht im QR-Code, wird nie an den Server gesendet).

## Lokale KI mit Ollama
Einstellungen → KI → Anbieter „Ollama“. Voraussetzung: Ollama von ollama.com installiert und ein Modell geladen
(z. B. `ollama pull qwen3`). Ollama-Modelle laufen offline und sind auch im Datenschutzmodus erlaubt.

## Architektur
```
jarvis/
  core/        Kern: Aktions-Registry (Risikostufen), Brain (lokal zuerst, dann KI), Config, DB, Log, Events
  modules/     je Fähigkeit ein Modul – neue Funktionen = neues Modul
    pc/        Programme, Fenster, Audio, Energie, Screenshots, Dateien, Web
    voice.py   Wake-Word (openWakeWord), STT (OpenAI / lokal Whisper), TTS (Neural / OpenAI / Windows)
    memory.py  Gedächtnis           automation.py  Profile, Routinen, eigene Befehle, Zeitplan, Ereignisse
    ai.py      OpenAI (Werkzeuge = dieselben Aktionen)   smarthome.py  Zimmer, Geräte, Home Assistant
    phone.py   Android (ADB), iPhone-Erkennung           remote.py     API für künftige Handy-App
    watcher.py aktiver Modus         maintenance.py  Backup, Diagnose       updater.py  Updates + Rollback
  ui/          Oberfläche (HTML/CSS/JS in WebView2)
```
Jede Fähigkeit ist eine **Aktion** mit Risikostufe (0 lesen … 3 kritisch). Parser, Profile, Routinen und KI nutzen
dieselben Aktionen – Sicherheit (Bestätigungen) und Protokoll sitzen an einer Stelle.

## Entwickeln
```
.venv\Scripts\python.exe run.py            # Start aus dem Quellcode
.venv\Scripts\python.exe run.py --debug    # mit Entwicklerwerkzeugen
powershell -File build.ps1                 # baut dist\JARVIS\JARVIS.exe + dist\JARVIS-Setup-x.y.z.exe + .sha256
```
Für ein neues Release: `VERSION` in `jarvis/__init__.py` erhöhen, `build.ps1` ausführen.

## Updates verteilen
In JARVIS unter Einstellungen → Updates die Adresse eines GitHub-Repositorys eintragen. Ein neues Release mit den
Dateien `JARVIS-Setup-x.y.z.exe` und `JARVIS-Setup-x.y.z.exe.sha256` genügt: JARVIS erkennt es, prüft die Prüfsumme,
sichert Daten und Programm, installiert still, startet neu – und rollt automatisch zurück, falls die neue Version nicht startet.
