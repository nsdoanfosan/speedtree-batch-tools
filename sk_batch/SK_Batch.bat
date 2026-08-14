@echo off
setlocal
rem SK Vegetation Batch GUI launcher (no console window)
set "GUARD=%~dp0..\launch_guard.pyw"
set "LAUNCHER=%~dp0sk_batch_gui.pyw"
set "COLLISION_DIR=%~dp0..\speedtree_collision_cli"
set "COLLISION_CLI=%COLLISION_DIR%\bin\speedtree_collision_cli.exe"
set "COLLISION_HOOK=%COLLISION_DIR%\bin\speedtree_collision_hook.dll"
set "SPEEDTREE_BATCH_LAUNCH_SOURCE=bat:SK_Batch.bat"

if exist "%COLLISION_CLI%" if exist "%COLLISION_HOOK%" goto collision_ready
echo [INFO] Building the SpeedTree post-collision CLI...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%COLLISION_DIR%\build.ps1"
if errorlevel 1 goto collision_build_failed

:collision_ready
"%COLLISION_CLI%" --diagnose >nul
if errorlevel 1 goto collision_diagnose_failed
set "SPEEDTREE_COLLISION_CLI_EXE=%COLLISION_CLI%"
set "SPEEDTREE_COLLISION_PERSISTENT=1"
if not defined SPEEDTREE_COLLISION_SESSION_ANCHOR set "SPEEDTREE_COLLISION_SESSION_ANCHOR=%USERPROFILE%\Downloads\blank.spm"
if not exist "%SPEEDTREE_COLLISION_SESSION_ANCHOR%" goto collision_anchor_missing

where.exe pythonw >nul 2>&1
if errorlevel 1 goto python_missing
if not exist "%GUARD%" goto guard_missing
if not exist "%LAUNCHER%" goto launcher_missing
pythonw -c "import tkinter" >nul 2>&1
if errorlevel 1 goto tkinter_missing

rem `start` guarantees process creation, not readiness. The guard creates its
rem Job supervisor before importing this GUI; its receipt records BAT source.
start "" /D "%~dp0" pythonw "%GUARD%" "%LAUNCHER%"
exit /b 0

:python_missing
echo [ERROR] pythonw was not found in PATH.
goto show_error

:guard_missing
echo [ERROR] Missing launch guard: "%GUARD%"
goto show_error

:launcher_missing
echo [ERROR] Missing launcher: "%LAUNCHER%"
goto show_error

:tkinter_missing
echo [ERROR] The pythonw found in PATH cannot import tkinter.
pythonw -c "import sys; print(sys.executable)"
goto show_error

:collision_build_failed
echo [ERROR] Failed to build the SpeedTree post-collision CLI.
goto show_error

:collision_diagnose_failed
echo [ERROR] The installed SpeedTree version is not supported by the collision CLI.
goto show_error

:collision_anchor_missing
echo [ERROR] Persistent SpeedTree anchor was not found: "%SPEEDTREE_COLLISION_SESSION_ANCHOR%"
goto show_error

:show_error
echo.
pause
exit /b 1
