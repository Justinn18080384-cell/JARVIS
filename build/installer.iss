; JARVIS – Installer (Inno Setup 6)
; Installiert pro Benutzer (keine Admin-Rechte nötig) nach %LOCALAPPDATA%\Programs\JARVIS.
; Persönliche Daten liegen getrennt in %APPDATA%\JARVIS und bleiben bei Updates erhalten.

#define AppName "JARVIS"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppExe "JARVIS.exe"

[Setup]
AppId={{7C1E4D2A-9B3F-4E8A-A6D5-JARVIS000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=JARVIS
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=JARVIS-Setup-{#AppVersion}
SetupIconFile=..\jarvis\assets\jarvis.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoDescription=JARVIS Installer

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Verknüpfungen:"
Name: "autostart"; Description: "JARVIS mit Windows starten (im Hintergrund)"; GroupDescription: "Start:"; Flags: unchecked

[InstallDelete]
; alte Programmdateien vollständig entfernen, damit nach Updates keine Reste bleiben
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\JARVIS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExe}"" --background"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "JARVIS jetzt starten"; Flags: nowait postinstall skipifsilent
; nach einem stillen Update automatisch neu starten
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExe} /F"; Flags: runhidden; RunOnceId: "KillJarvis"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // laufende Instanz sauber beenden
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM {#AppExe} /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(800);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // Autostart-Eintrag immer entfernen (auch wenn in JARVIS aktiviert)
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
    if SuppressibleMsgBox('Sollen auch deine persönlichen JARVIS-Daten gelöscht werden?' + #13#10 +
              '(Gedächtnis, Profile, Routinen, Geräte, Einstellungen, Backups, Sprachmodelle)' + #13#10#13#10 +
              'Wähle „Nein“, wenn du JARVIS später wieder installieren und alles behalten möchtest.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    begin
      DelTree(ExpandConstant('{userappdata}\JARVIS'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\JARVIS'), True, True, True);
    end;
  end;
end;
