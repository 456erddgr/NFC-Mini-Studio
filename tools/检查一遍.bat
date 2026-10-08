@echo off
chcp 65001 >nul
title NFC Mini Studio · 改完之后检查一遍
cd /d "%~dp0.."

echo ============================================================
echo  NFC Mini Studio · 实机自检（源码版 + 已安装打包版）
echo  机器人真的去点按钮、真的去扫蓝牙、真的去换壁纸
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] 没找到 python，请先装 Python 或把本脚本里的 python 换成完整路径
  pause
  exit /b 1
)

echo ---------- 1/2 源码版 ----------
python tools\verify_ui.py
set RC1=%errorlevel%

echo.
echo ---------- 2/2 已安装打包版 ----------
if exist "%LOCALAPPDATA%\Programs\NFCMiniStudio\NFC Mini Studio.exe" (
  python tools\verify_ui.py --exe "%LOCALAPPDATA%\Programs\NFCMiniStudio\NFC Mini Studio.exe"
  set RC2=%errorlevel%
) else (
  echo [!] 还没装到 %LOCALAPPDATA%\Programs\NFCMiniStudio，跳过
  set RC2=0
)

echo.
if "%RC1%"=="0" if "%RC2%"=="0" (
  echo [OK] 两版全部通过，可以收工。
) else (
  echo [!!] 有失败项，往上翻看红色 FAIL。
)
pause
