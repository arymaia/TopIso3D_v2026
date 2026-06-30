[Setup]
AppName=TopIso3D
AppVersion=2026
DefaultDirName={autopf}\TopIso3D
DefaultGroupName=TopIso3D
OutputDir=installer_output
OutputBaseFilename=TopIso3D_v2026_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=topiso3d.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\TopIso3D_v2026.exe"; DestDir: "{app}"; DestName: "TopIso3D_v2026.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\TopIso3D v2026"; Filename: "{app}\TopIso3D_v2026.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\TopIso3D v2026"; Filename: "{app}\TopIso3D_v2026.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\TopIso3D_v2026.exe"; WorkingDir: "{app}"; Description: "Launch TopIso3D"; Flags: nowait postinstall skipifsilent