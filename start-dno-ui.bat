@echo off
REM Copyright 2026 Froncort
REM
REM Licensed under the Apache License, Version 2.0 (the "License");
REM you may not use this file except in compliance with the License.
REM You may obtain a copy of the License at
REM
REM     http://www.apache.org/licenses/LICENSE-2.0
REM
REM Unless required by applicable law or agreed to in writing, software
REM distributed under the License is distributed on an "AS IS" BASIS,
REM WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
REM See the License for the specific language governing permissions and
REM limitations under the License.

cd /d "%~dp0"

set PY=
for %%P in (py -3.12 py -3.11 py -3.10 python3 python) do (
  %%P -c "import sys; exit(0 if sys.version_info[:2] >= (3,10) else 1)" 2>nul
  if not errorlevel 1 (
    set PY=%%P
    goto :found
  )
)

echo Python 3.10 or newer is required.
echo Download from https://www.python.org/downloads/
pause
exit /b 1

:found
echo Starting DNO UI with %PY% ...
echo Analysis logs will stream in this terminal.
set DNO_UI_LOG=1
set PYTHONUNBUFFERED=1
%PY% dno_ui.py
pause
