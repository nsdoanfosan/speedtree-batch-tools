@echo off
setlocal

rem Launch the integrated tabbed GUI without a console window.
set "GUARD=%~dp0launch_guard.pyw"
set "LAUNCHER=%~dp0speedtree_batch_tools_gui.pyw"
set "SPEEDTREE_BATCH_LAUNCH_SOURCE=bat:SpeedTree_Batch_Tools.bat"

where.exe pythonw >nul 2>&1
if errorlevel 1 goto python_missing
if not exist "%GUARD%" goto guard_missing
if not exist "%LAUNCHER%" goto launcher_missing

rem pythonw exits without opening a console, so this catches a PATH that
rem resolves to a Python without Tk before the window would silently fail.
pythonw -c "import tkinter" >nul 2>&1
if errorlevel 1 goto tkinter_missing

rem `start` guarantees process creation, not readiness. launch_guard.pyw creates
rem its Job supervisor before importing GUI code, so no worker can precede the
rem ownership boundary. The receipt records the BAT launch source.
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
echo         Install Python with the tcl/tk option, or fix the PATH order.
pythonw -c "import sys; print(sys.executable)"

:show_error
echo.
pause
exit /b 1
