@echo off
chcp 65001 >nul
title 智能客服装甲 - 一键打包与清理工具
cd /d "%~dp0"

echo ======================================================
echo 正在检查图标并开始打包 launcher.py
echo ======================================================

if exist "Ironman.ico" goto WITH_IRONMAN
if exist "1.ico" goto WITH_1_ICO
goto NO_ICON

:WITH_IRONMAN
echo 检测到 Ironman.ico 图标，正在执行带图标打包
pyinstaller --onefile --icon="Ironman.ico" launcher.py
goto AFTER_BUILD

:WITH_1_ICO
echo 检测到 1.ico 图标，正在执行带图标打包
pyinstaller --onefile --icon="1.ico" launcher.py
goto AFTER_BUILD

:NO_ICON
echo 未检测到图标文件，将使用默认图标打包
pyinstaller --onefile launcher.py
goto AFTER_BUILD

:AFTER_BUILD
if not exist "dist\launcher.exe" goto BUILD_FAIL

echo ======================================================
echo 正在清理临时文件并移动 exe
echo ======================================================

move /y "dist\launcher.exe" ".\客服助手.exe" >nul
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist launcher.spec del /q launcher.spec

echo ======================================================
echo 恭喜！客服助手.exe 已成功生成在当前目录！
echo ======================================================
pause
exit /b

:BUILD_FAIL
echo ======================================================
echo 打包失败，请检查上方的具体报错信息！
echo ======================================================
pause
exit /b