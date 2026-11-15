; JARVIS installer (Inno Setup 6). Built automatically by .github/workflows/build.yml
; Per-user install: no admin rights needed, and JARVIS can update itself.

#ifndef MyAppVersion
  #define MyAppVersion "0.0"
#endif

[Setup]
AppId={{7E4B2C1A-5D3F-4A8E-9B6C-JARVIS000001}
AppName=J.A.R.V.I.S
AppVersion={#MyAppVersion}
AppVerName=J.A.R.V.I.S {#MyAppVersion}
AppPublisher=MrSigal
DefaultDirName={localappdata}\Programs\JARVIS
DefaultGroupName=JARVIS
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=JARVIS-Setup
SetupIconFile=assets\jarvis.ico
UninstallDisplayIcon={app}\JARVIS.exe
UninstallDisplayName=J.A.R.V.I.S
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "hebrew"; MessagesFile: "compiler:Languages\Hebrew.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "קיצור דרך על שולחן העבודה"; GroupDescription: "קיצורי דרך:"
Name: "startup"; Description: "להפעיל את ג'רוויס אוטומטית כשהמחשב נדלק"; GroupDescription: "הפעלה:"; Flags: unchecked

[Files]
Source: "dist\JARVIS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\JARVIS"; Filename: "{app}\JARVIS.exe"; AppUserModelID: "MrSigal.JARVIS"
Name: "{userdesktop}\JARVIS"; Filename: "{app}\JARVIS.exe"; Tasks: desktopicon; AppUserModelID: "MrSigal.JARVIS"
Name: "{userstartup}\JARVIS"; Filename: "{app}\JARVIS.exe"; Tasks: startup

[InstallDelete]
; the old Python version's shortcut pointed at run_jarvis.bat
Type: files; Name: "{userdesktop}\JARVIS.lnk"

[Run]
Filename: "{app}\JARVIS.exe"; Description: "להפעיל את ג'רוויס עכשיו"; Flags: nowait postinstall

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
