@echo off
setlocal

rem Launch the integrated tabbed GUI without a console window.
set "GUARD=%~dp0launch_guard.pyw"
set "LAUNCHER=%~dp0speedtree_batch_tools_gui.pyw"

where.exe pythonw >nul 2>&1
if errorlevel 1 goto python_missing
if not exist "%GUARD%" goto guard_missing
if not exist "%LAUNCHER%" goto launcher_missing

rem pythonw exits without opening a console, so this catches a PATH that
rem resolves to a Python without Tk before the window would silently fail.
pythonw -c "import tkinter" >nul 2>&1
if errorlevel 1 goto tkinter_missing

rem `start` returns immediately and cannot report the child's exit code, so
rem launch_guard.pyw owns error reporting from here (message box + log).
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
