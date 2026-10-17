@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   QQ音乐加密音乐 解密 / 转换 工具
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python。
    echo 请先安装 Python 3.8 或更高版本: https://www.python.org/downloads/
    echo 安装时记得勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

python -c "import frida" >nul 2>nul
if errorlevel 1 (
    echo [提示] 缺少依赖 frida，正在尝试自动安装...
    python -m pip install frida
    if errorlevel 1 (
        echo [错误] 安装失败，请手动执行: python -m pip install frida
        pause
        exit /b 1
    )
)

python -c "import tkinterdnd2" >nul 2>nul
if errorlevel 1 (
    echo [提示] 缺少依赖 tkinterdnd2（拖拽功能需要），正在尝试自动安装...
    python -m pip install tkinterdnd2
)

echo 正在启动界面...
start "" pythonw "qqmusic_gui.py"
exit /b 0
