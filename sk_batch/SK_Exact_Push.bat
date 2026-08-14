@echo off
setlocal
set "COLLISION_DIR=%~dp0..\speedtree_collision_cli"
set "COLLISION_CLI=%COLLISION_DIR%\bin\speedtree_collision_cli.exe"
set "COLLISION_HOOK=%COLLISION_DIR%\bin\speedtree_collision_hook.dll"

if exist "%COLLISION_CLI%" if exist "%COLLISION_HOOK%" goto collision_ready
echo [INFO] Building the SpeedTree post-collision CLI...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%COLLISION_DIR%\build.ps1"
if errorlevel 1 goto collision_build_failed

:collision_ready
"%COLLISION_CLI%" --diagnose
if errorlevel 1 goto collision_diagnose_failed
set "SPEEDTREE_COLLISION_CLI_EXE=%COLLISION_CLI%"
set "SPEEDTREE_COLLISION_PERSISTENT=1"
if not defined SPEEDTREE_COLLISION_SESSION_ANCHOR set "SPEEDTREE_COLLISION_SESSION_ANCHOR=%USERPROFILE%\Downloads\blank.spm"
if not exist "%SPEEDTREE_COLLISION_SESSION_ANCHOR%" goto collision_anchor_missing
python "%~dp0exact_push.py" %*
set "SK_EXACT_PUSH_EXIT=%errorlevel%"
"%COLLISION_CLI%" --shutdown-session >nul 2>&1
exit /b %SK_EXACT_PUSH_EXIT%

:collision_build_failed
echo [ERROR] Failed to build the SpeedTree post-collision CLI.
exit /b 10

:collision_diagnose_failed
echo [ERROR] The installed SpeedTree version is not supported by the collision CLI.
exit /b 11

:collision_anchor_missing
echo [ERROR] Persistent SpeedTree anchor was not found: "%SPEEDTREE_COLLISION_SESSION_ANCHOR%"
exit /b 12
