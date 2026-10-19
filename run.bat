@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo Не найден Python. Поставь Python 3.10+ с python.org ^(галочка "Add python.exe to PATH"^) и запусти снова.
  pause
  exit /b 1
)
:start
%PY% nav_tool.py %*
rem код 3 = программа обновилась и просит перезапуск
if "%errorlevel%"=="3" goto start
pause
