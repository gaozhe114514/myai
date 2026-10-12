@echo off
REM ============================================================
REM  Double-click to start the MyAI knowledge assistant.
REM
REM  Equivalent to typing these in cmd:
REM      cd /d F:\ClaudecodeDemo1
REM      .venv\Scripts\activate
REM      streamlit run app.py
REM
REM  NOTE: keep this file ASCII-only.
REM  cmd.exe reads .cmd files using the system codepage (GBK on
REM  Chinese Windows). If you put UTF-8 Chinese in here, the bytes
REM  get mis-decoded and the line breaks get eaten -> errors like
REM  "'xxx' is not recognized as an internal or external command".
REM ============================================================

REM %~dp0 = the folder this script lives in.
REM Using it means no hard-coded path, so it keeps working if moved.
cd /d "%~dp0"

echo.
echo   MyAI  -  starting web UI ...
echo   ----------------------------------------
echo.

REM Call the venv's python directly, so we skip the "activate" step.
REM Use "-m streamlit" rather than streamlit.exe -- more reliable.
.venv\Scripts\python.exe -m streamlit run app.py --server.headless false

REM Keep the window open so errors stay readable.
echo.
echo   Server stopped.
pause
