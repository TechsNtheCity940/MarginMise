; MarginMise Windows Installer
; Creates desktop/start menu shortcuts for professional deployment

!include "MUI2.nsh"

Name "MarginMise"
OutFile "MarginMise-Installer.exe"
InstallDir "$LOCALAPPDATA\MarginMise"
RequestExecutionLevel user

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "..\dist\MarginMise\*.*"

  ; Desktop shortcut
  CreateShortCut "$DESKTOP\MarginMise.lnk" "$INSTDIR\MarginMise.exe" "" "$INSTDIR\assets\favicon.ico" 0

  ; Start Menu shortcut
  CreateDirectory "$SMPROGRAMS\MarginMise"
  CreateShortCut "$SMPROGRAMS\MarginMise\MarginMise.lnk" "$INSTDIR\MarginMise.exe" "" "$INSTDIR\assets\favicon.ico" 0
  CreateShortCut "$SMPROGRAMS\MarginMise\Uninstall.lnk" "$INSTDIR\uninstall.exe" "" "$INSTDIR\uninstall.exe" 0

  ; Uninstaller
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Registry entries for Add/Remove Programs
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise" "DisplayName" "MarginMise"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise" "InstallLocation" "$INSTDIR"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\MarginMise.lnk"
  Delete "$SMPROGRAMS\MarginMise\MarginMise.lnk"
  Delete "$SMPROGRAMS\MarginMise\Uninstall.lnk"
  RMDir "$SMPROGRAMS\MarginMise"
  Delete "$INSTDIR\uninstall.exe"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise"
  RMDir /r "$INSTDIR"
SectionEnd
