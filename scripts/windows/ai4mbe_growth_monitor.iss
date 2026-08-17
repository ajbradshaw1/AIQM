#ifndef MyAppVersion
  #error MyAppVersion must be supplied to ISCC
#endif
#ifndef PayloadRoot
  #error PayloadRoot must be supplied to ISCC
#endif
#ifndef OutputDir
  #error OutputDir must be supplied to ISCC
#endif

#define MyAppName "AI4MBE Growth Monitor"
#define MyAppPublisher "Yang Group"
#define MyAppId "{{A20F8D5E-7D0A-47C6-9BB6-9B187E6FE06A}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\AI4MBE-Growth-Monitor
UsePreviousAppDir=yes
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
OutputDir={#OutputDir}
OutputBaseFilename=AI4MBE-Growth-Monitor-{#MyAppVersion}-Windows-x64-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
Uninstallable=yes
UninstallDisplayName={#MyAppName}
UninstallFilesDir={localappdata}\AI4MBE\Uninstall
MinVersion=10.0.17763
SetupLogging=yes
SetupIconFile={#PayloadRoot}\assets\ai4mbe_app_icon.ico
UninstallDisplayIcon={app}\assets\ai4mbe_app_icon.ico

[Tasks]
Name: "desktopicon"; Description: "Create Desktop shortcuts"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{tmp}\AI4MBE-Payload"; Flags: ignoreversion recursesubdirs createallsubdirs deleteafterinstall

[Icons]
Name: "{group}\O-MBE Growth Monitor"; Filename: "{app}\Start O-MBE Growth Monitor.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai4mbe_app_icon.ico"
Name: "{group}\Ch-MBE Growth Monitor"; Filename: "{app}\Start Ch-MBE Growth Monitor.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai4mbe_app_icon.ico"
Name: "{group}\RHEED Post-processing Labeler"; Filename: "{app}\Start RHEED Post-processing Labeler.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai4mbe_app_icon.ico"
Name: "{group}\AI4MBE Operator Manual"; Filename: "{app}\docs\RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf"
Name: "{group}\Uninstall AI4MBE Growth Monitor"; Filename: "{uninstallexe}"
Name: "{autodesktop}\O-MBE Growth Monitor"; Filename: "{app}\Start O-MBE Growth Monitor.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai4mbe_app_icon.ico"; Tasks: desktopicon
Name: "{autodesktop}\Ch-MBE Growth Monitor"; Filename: "{app}\Start Ch-MBE Growth Monitor.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai4mbe_app_icon.ico"; Tasks: desktopicon
Name: "{autodesktop}\RHEED Post-processing Labeler"; Filename: "{app}\Start RHEED Post-processing Labeler.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\assets\ai4mbe_app_icon.ico"; Tasks: desktopicon
Name: "{autodesktop}\AI4MBE Operator Manual"; Filename: "{app}\docs\RHEED_GUI_Postprocessing_Labeling_User_Manual_EN.pdf"; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  PowerShellPath, ScriptPath, Parameters: String;
begin
  if CurStep = ssPostInstall then
  begin
    PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
    ScriptPath := ExpandConstant('{tmp}\AI4MBE-Payload\scripts\windows\install_ai4mbe.ps1');
    Parameters := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath +
      '" -SourceRoot "' + ExpandConstant('{tmp}\AI4MBE-Payload') +
      '" -InstallRoot "' + ExpandConstant('{app}') +
      '" -DataRoot "' + ExpandConstant('{userdocs}\AI4MBE\GrowthSessions') +
      '" -ManagedUninstallerPath "' + ExpandConstant('{uninstallexe}') +
      '" -NoShortcuts -Quiet';
    if (not Exec(PowerShellPath, Parameters, '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
    begin
      RaiseException('AI4MBE runtime configuration failed. See the installer log under LocalAppData\\AI4MBE\\InstallerLogs.');
    end;
  end;
end;
