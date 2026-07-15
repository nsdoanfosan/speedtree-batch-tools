@echo off
setlocal

rem Launch the integrated tabbed GUI without a console window.
set "LAUNCHER=%~dp0speedtree_batch_tools_gui.pyw"

where.exe pythonw >nul 2>&1
if errorlevel 1 goto python_missing
if not exist "%LAUNCHER%" goto launcher_missing

start "" /D "%~dp0" pythonw "%LAUNCHER%"
if errorlevel 1 goto launch_failed
exit /b 0

:python_missing
echo [ERROR] pythonw was not found in PATH.
goto show_error

:launcher_missing
echo [ERROR] Missing launcher: "%LAUNCHER%"
goto show_error

:launch_failed
echo [ERROR] The integrated SpeedTree Batch Tools window could not be started.

:show_error
echo.
pause
exit /b 1
