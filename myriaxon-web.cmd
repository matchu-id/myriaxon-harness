@echo off
REM Myriaxon -- the local web dashboard.
REM
REM   myriaxon-web.cmd                       serve this repository, open a browser
REM   myriaxon-web.cmd --port 9000           choose a port
REM   myriaxon-web.cmd --workspace C:\x      serve a different project
REM   myriaxon-web.cmd --no-browser          do not open a browser
REM
REM Kept separate from myriaxon.cmd on purpose: the dashboard is a long-running server, while myriaxon.cmd runs
REM the terminal agent and exits. Two different lifecycles should not share one command.
REM
REM The workspace defaults to the repository rather than the current directory. Running `myriaxon-web.cmd`
REM from C:\Users\<you> otherwise made the home directory the workspace, which fails confusingly: the
REM config and uploads directories are not writable there, so startup crashed with a PermissionError
REM on ...\.myriaxon\uploads, and even once that was fixed the agent would have had nowhere to write
REM files. A project root is what the dashboard is for, so that is the default.
setlocal
set "REPO=%~dp0"
if "%MYRIAXON_CONFIG_DIR%"=="" set "MYRIAXON_CONFIG_DIR=%REPO%.myriaxon"
if not exist "%MYRIAXON_CONFIG_DIR%" mkdir "%MYRIAXON_CONFIG_DIR%" 2>nul

REM Add --workspace only when the caller did not supply one.
set "HAVE_WORKSPACE="
for %%A in (%*) do (
  if /I "%%~A"=="--workspace" set "HAVE_WORKSPACE=1"
)
if defined HAVE_WORKSPACE (
  "%REPO%.venv\Scripts\python.exe" -m myriaxon web %*
) else (
  "%REPO%.venv\Scripts\python.exe" -m myriaxon web --workspace "%REPO%." %*
)
endlocal
