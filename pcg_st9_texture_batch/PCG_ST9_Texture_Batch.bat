@echo off
setlocal
rem PCG ST9 texture batch GUI launcher (no console window)
set "GUARD=%~dp0..\launch_guard.pyw"
set "LAUNCHER=%~dp0pcg_texture_gui.pyw"
set "HEADLESS=%~dp0exact_target_repair.py"
set "SPEEDTREE_BATCH_LAUNCH_SOURCE=bat:PCG_ST9_Texture_Batch.bat"

where.exe pythonw >nul 2>&1
if errorlevel 1 goto python_missing
if not exist "%GUARD%" goto guard_missing
if not exist "%LAUNCHER%" goto launcher_missing
if not exist "%HEADLESS%" goto launcher_missing
pythonw -c "import tkinter" >nul 2>&1
if errorlevel 1 goto tkinter_missing

if not "%~1"=="" goto headless

rem `start` guarantees process creation, not readiness. The guard creates its
rem Job supervisor before importing this GUI; its receipt records BAT source.
start "" /D "%~dp0" pythonw "%GUARD%" "%LAUNCHER%"
exit /b 0

:headless
rem Keep every quoted/Unicode/repeated argument intact.  The bounded command
rem stays synchronous so its durable receipt and exit status are observable.
pythonw "%GUARD%" "%HEADLESS%" %*
exit /b %errorlevel%

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

:show_error
echo.
pause
exit /b 1
