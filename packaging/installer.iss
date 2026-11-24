; THS-T-Assistant —— Inno Setup 6 安装包脚本
;
; 由 CI 通过命令行传入参数：
;   ISCC.exe /DAppVersion=1.0.0-20261124-abc1234 ^
;            /DSourceDir=C:\path\to\dist\release\THS-T-Assistant-...-portable ^
;            /DOutDir=C:\path\to\dist\release ^
;            packaging\installer.iss
;
; 默认安装到当前用户目录（{localappdata}），无需管理员权限；
; 这样主程序同目录的 config.json 对普通用户可写（配置编辑器保存需要）。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\release\portable"
#endif
#ifndef OutDir
  #define OutDir "..\dist\release"
#endif

#define AppName "THS-T-Assistant"
#define AppPublisher "gannikus"
#define AppExe "THS-T-Assistant.exe"

[Setup]
AppId={{6F3C2A91-8B4D-4E7A-9D12-5A0E7C3F1B88}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/gannikus/ths-t-trader
AppSupportURL=https://github.com/gannikus/ths-t-trader/issues
AppUpdatesURL=https://github.com/gannikus/ths-t-trader/releases
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
OutputDir={#OutDir}
OutputBaseFilename={#AppName}-{#AppVersion}-setup
SetupIconFile={#SourceDir}\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
DisableProgramGroupPage=yes
LicenseFile=

[Languages]
; Chocolatey 版 Inno Setup 6.7.1（非商业版）不含 ChineseSimplified.isl，
; 只用内置 Default.isl，避免 CI 编译失败。安装内容本身仍为中文。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他快捷方式:"; Flags: unchecked
Name: "quicklaunchicon"; Description: "创建快速启动栏快捷方式"; GroupDescription: "其他快捷方式:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "{#SourceDir}\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\app.ico"; DestDir: "{app}"; DestName: "app.ico"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\app.ico"
Name: "{group}\配置编辑器"; Filename: "{app}\{#AppExe}"; Parameters: "--ui"; IconFilename: "{app}\app.ico"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\app.ico"; Tasks: quicklaunchicon
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent unchecked

; 升级安装时 config.json 使用 onlyifdoesntexist：用户已有配置（持仓成本等私有数据）
; 不会被覆盖，出厂示例配置只在首次安装时落地。
