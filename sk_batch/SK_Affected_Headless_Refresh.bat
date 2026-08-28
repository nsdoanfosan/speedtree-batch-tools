@echo off
setlocal
set "COLLISION_DIR=%~dp0..\speedtree_collision_cli"
set "COLLISION_CLI=%COLLISION_DIR%\bin\speedtree_collision_cli.exe"
set "COLLISION_HOOK=%COLLISION_DIR%\bin\speedtree_collision_hook.dll"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%COLLISION_DIR%\build.ps1" -IfNeeded
if errorlevel 1 goto collision_build_failed

"%COLLISION_CLI%" --diagnose 2>nul | %SystemRoot%\System32\findstr.exe /x /c:"SPEEDTREE_COLLISION_CLI_CONTRACT=native-runtime-receipt-v14"
if errorlevel 1 goto collision_diagnose_failed

set "SPEEDTREE_BATCH_LAUNCH_SOURCE=bat:SK_Affected_Headless_Refresh.bat"
set "SPEEDTREE_COLLISION_CLI_EXE=%COLLISION_CLI%"
set "SPEEDTREE_COLLISION_NATIVE_CLI=1"
set "SPEEDTREE_COLLISION_PERSISTENT=0"
python "%~dp0affected_headless_refresh.py" %*
exit /b %errorlevel%

:collision_build_failed
echo [ERROR] Failed to build the SpeedTree native export CLI.
exit /b 10

:collision_diagnose_failed
echo [ERROR] The installed SpeedTree/native hook contract is not v13.
exit /b 11
