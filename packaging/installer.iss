; Inno Setup script for the AI Gold Trading Decision Assistant.
;
; Produces a single Setup.exe that installs the application per-user (no admin
; rights needed), creates Start Menu and Desktop shortcuts, registers an
; uninstaller in Add/Remove Programs, and leaves the user's data alone on
; uninstall.
;
; Build with:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss

#define AppName        "Gold Decision Assistant"
#define AppShortName   "GoldAssistant"
#define AppVersion     "1.3.1"
#define AppPublisher   "Joseph"
#define AppExeName     "GoldAssistant.exe"

[Setup]
AppId={{8B4E2C71-9F3A-4D62-A5E8-1C7B0D9F4A23}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist
OutputBaseFilename=GoldAssistant-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=goldassistant.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
; Per-user install so no administrator prompt is needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=DISCLAIMER.txt

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Start automatically when I log in"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The whole PyInstaller one-folder bundle.
Source: "..\dist\GoldAssistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\{#AppName} (open in browser)"; Filename: "{app}\{#AppExeName}"; Parameters: "--browser"
Name: "{group}\{#AppName} (debug console)"; Filename: "{app}\GoldAssistant-debug.exe"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove only what the installer created. The trading database, journal and
; settings live in %USERPROFILE%\.goldassist and are deliberately preserved --
; that is the user's own trading history.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
const
  CRLF = #13#10;

// Warn if the MetaTrader 5 terminal is not present: the application cannot
// read market data without it, and finding that out after install is worse
// than being told during it.
function InitializeSetup(): Boolean;
var
  Found: Boolean;
begin
  Found := DirExists(ExpandConstant('{commonpf}\MetaTrader 5')) or
           DirExists(ExpandConstant('{commonpf}\MetaTrader 5 EXNESS')) or
           DirExists(ExpandConstant('{commonpf32}\MetaTrader 5')) or
           DirExists(ExpandConstant('{localappdata}\Programs\MetaTrader 5'));
  if not Found then
  begin
    if MsgBox('MetaTrader 5 was not found on this computer.' +
              CRLF + CRLF +
              'This application reads its Gold price data from a running MT5 ' +
              'terminal, and will not be able to analyse anything without one.' +
              CRLF + CRLF + 'Install anyway?',
              mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := False;
      Exit;
    end;
  end;
  Result := True;
end;
