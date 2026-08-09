@echo off
setlocal
python "%~dp0exact_push.py" %*
exit /b %errorlevel%
