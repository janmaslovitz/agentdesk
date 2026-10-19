; Inno Setup script for the AgentDesk installer. Build with ..\build_installer.ps1 (ISCC /DAppVersion=x.y).
; It packs only dist\AgentDesk (the PyInstaller program): never data\, logins, keys, projects, .venv or worktrees.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6B0F2C2E-5C57-4B8E-9A55-AD0A6E1D2F12}
AppName=AgentDesk
AppVersion={#AppVersion}
AppPublisher=AgentDesk
DefaultDirName={autopf}\AgentDesk
DefaultGroupName=AgentDesk
DisableDirPage=no
DisableProgramGroupPage=yes
; Admin by default (Program Files). "Install for me only" in the dialog, or /CURRENTUSER, runs without
; elevation; {autopf} is then %LOCALAPPDATA%\Programs.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\agentdesk\web\agentdesk.ico
UninstallDisplayIcon={app}\AgentDesk.exe
OutputDir=..\dist\installer
OutputBaseFilename=AgentDesk-Setup-{#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Restart Manager can't close AgentDesk's own helper processes (the bundled claude.exe has no window), so "force close"
; used to hang. NextButtonClick below ends everything running from the install folder first.
CloseApplications=no
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; Program only. Nothing named data, accounts, .env, .venv, worktrees or __pycache__ goes in.
Source: "..\dist\AgentDesk\*"; DestDir: "{app}"; Excludes: "data,accounts,.env,.venv,worktrees,__pycache__,*.pyc"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\AgentDesk"; Filename: "{app}\AgentDesk.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\AgentDesk"; Filename: "{app}\AgentDesk.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\AgentDesk.exe"; Description: "Start AgentDesk"; Flags: nowait postinstall skipifsilent

; No [UninstallDelete] for data: the uninstaller removes only the files it installed. A data\ folder next to
; the .exe is never touched; %LOCALAPPDATA%\AgentDesk is removed only if the user says so (below).

[Code]
function ClaudeInstalled(): Boolean;
begin
  Result := (FileSearch('claude.exe', GetEnv('PATH')) <> '') or (FileSearch('claude.cmd', GetEnv('PATH')) <> '')
    or FileExists(ExpandConstant('{%USERPROFILE}\.local\bin\claude.exe'));
end;

function WebView2Installed(): Boolean;
var
  v: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v)
    or RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v)
    or RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) then
    Result := (v <> '') and (v <> '0.0.0.0');
end;

procedure OfferLink(const Msg, Url: String);
var
  rc: Integer;
begin
  if MsgBox(Msg + #13#10#13#10 + 'Open the download page now? Setup continues either way.', mbInformation, MB_YESNO) = IDYES then
    ShellExec('open', Url, '', '', SW_SHOWNORMAL, ewNoWait, rc);
end;

procedure StopAppProcesses(const Dir: String);
var
  rc: Integer;
  ps: String;
begin
  ps := '$d = ''' + RemoveBackslash(Dir) + '\*''; ' +
        'Get-Process | Where-Object { $_.Path -like $d } | Stop-Process -Force -ErrorAction SilentlyContinue';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), '-NoProfile -NonInteractive -Command "' + ps + '"',
    '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(500);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  if CurPageID = wpReady then
    StopAppProcesses(WizardDirValue);
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardSilent then
  begin
    if not ClaudeInstalled() then
      OfferLink('Claude Code CLI was not found. AgentDesk needs it (and "claude login") to run agents. ' +
        'Setup does not install it.', 'https://docs.claude.com/en/docs/claude-code/setup');
    if not WebView2Installed() then
      OfferLink('Microsoft Edge WebView2 Runtime was not found. AgentDesk needs it for its window (Windows 11 has it); ' +
        'without it AgentDesk opens in your browser instead.', 'https://developer.microsoft.com/microsoft-edge/webview2/');
  end;
end;

function InitializeUninstall(): Boolean;
begin
  StopAppProcesses(ExpandConstant('{app}'));
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  d: String;
  remove: Boolean;
begin
  if CurUninstallStep = usUninstall then
  begin
    d := ExpandConstant('{localappdata}\AgentDesk');
    if DirExists(d) then
    begin
      if UninstallSilent then
        remove := Pos('/REMOVEDATA', Uppercase(GetCmdTail)) > 0  // silent: keep the data unless asked
      else
        remove := MsgBox('Also remove my data (logins, projects, settings) in' + #13#10 + d + ' ?' + #13#10#13#10 +
          'Choose No to keep it.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
      if remove then
        DelTree(d, True, True, True);
    end;
  end;
end;
