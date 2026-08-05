@echo off
setlocal

rem Push-independent artifact retention. With no arguments this only writes a
rem dry-run JSON plan for logs, retry receipts, and production backup inventory.
set "GUARD=%~dp0launch_guard.pyw"
set "MAINTENANCE=%~dp0artifact_retention.py"
set "SPEEDTREE_BATCH_LAUNCH_SOURCE=bat:SpeedTree_Artifact_Maintenance.bat"

where.exe python >nul 2>&1
if errorlevel 1 goto python_missing
if not exist "%GUARD%" goto guard_missing
if not exist "%MAINTENANCE%" goto script_missing

rem /WAIT preserves the maintenance command's exit code while the shared
rem launch guard owns child lifetime and records the BAT launch source.
start "" /D "%~dp0" /WAIT /B python "%GUARD%" "%MAINTENANCE%" %*
set "MAINTENANCE_EXIT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %MAINTENANCE_EXIT%

:python_missing
echo [ERROR] python was not found in PATH.
goto show_error

:guard_missing
echo [ERROR] Missing launch guard: "%GUARD%"
goto show_error

:script_missing
echo [ERROR] Missing maintenance script: "%MAINTENANCE%"

:show_error
echo.
pause
exit /b 1
