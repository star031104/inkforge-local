#define MyAppName "砚火 InkForge"
#define MyAppVersion "0.33.1"
#define MyAppExeName "InkForge.exe"

[Setup]
AppId={{DAB63B17-6904-483A-AB04-AB812760ADCB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\InkForge
DefaultGroupName={#MyAppName}
OutputDir=..\dist\installer
OutputBaseFilename=InkForge-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\dist\InkForge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
