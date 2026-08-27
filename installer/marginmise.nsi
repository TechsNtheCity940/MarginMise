; MarginMise Windows Installer - Phased Installation
; Creates desktop/start menu shortcuts for the folder-based build

!include "MUI2.nsh"

Name "MarginMise"
OutFile "MarginMise-Installer.exe"
InstallDir "$LOCALAPPDATA\MarginMise"
SetCompressor lzma
ShowInstDetails show

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Section "Install"
  ; Install files
  SetOutPath "$INSTDIR"
  File /r "dist\MarginMise\*.*"
  
  ; Create desktop shortcut
  CreateShortCut "$DESKTOP\MarginMise.lnk" "$INSTDIR\MarginMise.exe" "" "$INSTDIR\assets\app_icon_256.png" 0
  
  ; Create start menu entry
  CreateDirectory "$SMPROGRAMS\MarginMise"
  CreateShortCut "$SMPROGRAMS\MarginMise\MarginMise.lnk" "$INSTDIR\MarginMise.exe" "" "$INSTDIR\assets\app_icon_256.png" 0
  CreateShortCut "$SMPROGRAMS\MarginMise\Uninstall.lnk" "$INSTDIR\uninstall.exe" "" "$INSTDIR\uninstall.exe" 0
  
  ; Write uninstaller
  WriteUninstaller "$INSTDIR\uninstall.exe"
  
  ; Write registry keys for uninstall
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise" "DisplayName" "MarginMise"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise" "DisplayIcon" "$INSTDIR\assets\app_icon_256.png"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise" "Publisher" "MarginMise"
SectionEnd

Section "Uninstall"
  ; Remove files
  RMDir /r "$INSTDIR"
  
  ; Remove shortcuts
  Delete "$DESKTOP\MarginMise.lnk"
  RMDir /r "$SMPROGRAMS\MarginMise"
  
  ; Remove registry keys
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarginMise"
SectionEnd
