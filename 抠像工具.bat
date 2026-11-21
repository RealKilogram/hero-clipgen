@echo off
chcp 936 >nul
cd /d "%~dp0"
title 视频抠像工具

set "PY="

if exist "%~dp0runtime\python.exe" (
  set "PY=%~dp0runtime\python.exe"
  goto RUN
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 --version >nul 2>nul
  if not errorlevel 1 (
    set "PY=py -3"
    goto RUN
  )
)

for /f "delims=" %%i in ('where python 2^>nul') do (
  echo %%i | findstr /i "WindowsApps" >nul
  if errorlevel 1 (
    if not defined PY set "PY=%%i"
  )
)

if defined PY goto RUN

echo.
echo   [没找到 Python]
echo.
echo   装了 Python 但没勾 "Add Python to PATH" 的话，重装一次并勾上。
echo   没装的话去这里下载（3.9 或更高）：
echo     https://www.python.org/downloads/
echo.
pause
exit /b 1

:RUN
echo 使用 Python: %PY%
echo.
echo 正在启动界面，浏览器会自动打开 http://127.0.0.1:17331/
echo 关闭这个黑窗口就退出工具。
echo.
%PY% -u gui.py
echo.
echo 工具已退出。
pause
exit /b 0