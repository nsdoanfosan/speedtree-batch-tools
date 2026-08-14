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
python "%~dp0exact_push.py" %*
exit /b %errorlevel%

:collision_build_failed
echo [ERROR] Failed to build the SpeedTree post-collision CLI.
exit /b 10

:collision_diagnose_failed
echo [ERROR] The installed SpeedTree version is not supported by the collision CLI.
exit /b 11
