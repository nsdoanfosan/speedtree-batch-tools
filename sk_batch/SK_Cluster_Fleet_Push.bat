@echo off
setlocal
python "%~dp0cluster_fleet_push.py" %*
exit /b %errorlevel%
