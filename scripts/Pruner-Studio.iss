#ifndef AppVersion
  #error Supply /DAppVersion=x.y.z
#endif

[Setup]
AppId=PrunerStudio
AppName=Pruner Studio
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}.0
UninstallDisplayIcon={app}\Pruner-Studio.exe
DefaultDirName={localappdata}\Programs\Pruner Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Pruner-Studio-v{#AppVersion}-win64-setup
Compression=lzma2
WizardStyle=modern

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\Pruner-Studio.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\Pruner Studio"; Filename: "{app}\Pruner-Studio.exe"
Name: "{userdesktop}\Pruner Studio"; Filename: "{app}\Pruner-Studio.exe"; Tasks: desktopicon

[Run]
Filename: "https://developer.microsoft.com/microsoft-edge/webview2/"; Description: "WebView2 is missing. Open Microsoft's download page for the desktop window"; Flags: postinstall shellexec skipifsilent unchecked; Check: MissingRuntime

[Code]
function HasRuntime(Root: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
            and (Version <> '') and (Version <> '0.0.0.0');
end;

function MissingRuntime: Boolean;
begin
  Result := not (HasRuntime(HKLM32) or HasRuntime(HKCU));
end;
