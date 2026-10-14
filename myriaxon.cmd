@echo off
REM Myriaxon -- the terminal agent (interactive REPL or one-shot).
REM
REM   myriaxon.cmd                 interactive REPL (recommended: pays model warm-up once)
REM   myriaxon.cmd -p "say hi"     one-shot, exits when done
REM   myriaxon.cmd --help
REM
REM For the web dashboard use myriaxon-web.cmd instead.
setlocal
set "REPO=%~dp0"
if "%MYRIAXON_CONFIG_DIR%"=="" set "MYRIAXON_CONFIG_DIR=%REPO%.myriaxon"
if not exist "%MYRIAXON_CONFIG_DIR%" mkdir "%MYRIAXON_CONFIG_DIR%" 2>nul
"%REPO%.venv\Scripts\python.exe" -m myriaxon %*
endlocal
