; OBS Stream Doctor - Windows kurulum betigi (NSIS 3)
; Derlemek icin: scripts/build_windows_installer.sh
Unicode true
!include "MUI2.nsh"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef APPDIR
  !define APPDIR "..\build\app"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\build\OBS-Stream-Doctor-Kurulum.exe"
!endif

!define APPNAME "OBS Stream Doctor"
!define REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\OBSStreamDoctor"
!define RUNARGS '-I -X utf8 "$INSTDIR\obs_stream_doctor.py" --pause-at-end'

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\OBSStreamDoctor"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "${APPNAME} ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1055 "ProductName" "${APPNAME}"
VIAddVersionKey /LANG=1055 "FileDescription" "${APPNAME} Kurulumu"
VIAddVersionKey /LANG=1055 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1055 "LegalCopyright" "MIT License"

!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "${APPNAME}"
!define MUI_WELCOMEPAGE_TEXT "OBS ile Kick'te yayın yaparken oluşan donmaların sebebini bulur.$\r$\n$\r$\nYayın sırasında her saniye OBS'i, modemi, operatörü ve Kick sunucusunu ölçer. Donma olduğunda sorunun evde mi, operatörde mi yoksa Kick tarafında mı olduğunu gösterir.$\r$\n$\r$\nBaşka bir şey kurmanıza gerek yok. Devam etmek için İleri'ye tıklayın."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "Kurulum tamamlandı"
!define MUI_FINISHPAGE_TEXT "Masaüstünde '${APPNAME}' simgesi oluşturuldu.$\r$\n$\r$\nKullanım:$\r$\n1. Yayını açmadan önce simgeye çift tıklayın.$\r$\n2. Tarayıcıda canlı panel açılır; siyah pencere açık kalsın.$\r$\n3. Yayın bitince siyah pencerede Ctrl+C tuşlarına basın.$\r$\n$\r$\nRaporlar masaüstündeki '${APPNAME} Raporları' klasörüne kaydedilir."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Şimdi başlat"
!define MUI_FINISHPAGE_RUN_FUNCTION StartApp
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Turkish"

Function StartApp
  SetOutPath "$INSTDIR"
  Exec '"$INSTDIR\python\python.exe" ${RUNARGS}'
FunctionEnd

Section "Kur"
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\python"
  File /r "${APPDIR}\*"
  File "icon.ico"

  SetOutPath "$INSTDIR"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\python\python.exe" '${RUNARGS}' "$INSTDIR\icon.ico" 0 SW_SHOWNORMAL "" "OBS yayın donma teşhisi"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\python\python.exe" '${RUNARGS}' "$INSTDIR\icon.ico" 0
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Kaldır.lnk" "$INSTDIR\Kaldir.exe"

  WriteUninstaller "$INSTDIR\Kaldir.exe"
  WriteRegStr HKCU "${REGKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${REGKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${REGKEY}" "DisplayIcon" "$INSTDIR\icon.ico"
  WriteRegStr HKCU "${REGKEY}" "UninstallString" '"$INSTDIR\Kaldir.exe"'
  WriteRegStr HKCU "${REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${REGKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${REGKEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${REGKEY}"
SectionEnd
