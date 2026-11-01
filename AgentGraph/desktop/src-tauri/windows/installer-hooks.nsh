!macro NSIS_HOOK_PREINSTALL
  ; Ask the running client to quit through its own single-instance path;
  ; force-killing leaves a ghost tray icon behind on Windows.
  DetailPrint "Closing the running Veynod client..."
  nsExec::ExecToStack 'cmd /c tasklist /FI "IMAGENAME eq veynod-desktop.exe" /NH | find /I "veynod-desktop.exe"'
  Pop $0
  StrCmp $0 "0" 0 veynod_install_done
  ; Exec is non-blocking: an older build may ignore --quit and stay open,
  ; so never wait on that process; force-close whatever is left.
  Exec '"$INSTDIR\veynod-desktop.exe" --quit'
  Sleep 1500
  nsExec::Exec 'taskkill /IM veynod-desktop.exe /F'
  Pop $0
  Sleep 300
  veynod_install_done:
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Closing the running Veynod client..."
  nsExec::ExecToStack 'cmd /c tasklist /FI "IMAGENAME eq veynod-desktop.exe" /NH | find /I "veynod-desktop.exe"'
  Pop $0
  StrCmp $0 "0" 0 veynod_uninstall_done
  Exec '"$INSTDIR\veynod-desktop.exe" --quit'
  Sleep 1500
  nsExec::Exec 'taskkill /IM veynod-desktop.exe /F'
  Pop $0
  Sleep 300
  veynod_uninstall_done:
!macroend
