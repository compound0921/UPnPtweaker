@echo off
rem 打包 UPnPtweaker 为单文件 exe。产物 dist\UPnPtweaker.exe 双击即用,
rem 拿到 exe 的人不需要装 Python。
rem
rem 打包工具要先装一次(只影响你这台打包机,不影响用 exe 的人):
rem     python -m pip install pyinstaller
rem
rem 注意:这里用 --windowed,双击只弹图形界面、不带控制台窗口;
rem 代价是用 exe 跑命令行子命令(scan/list/add…)看不到输出。
rem 如果需要命令行输出,把下面的 --windowed 去掉重新打包即可。
cd /d "%~dp0"

rem 和 start.bat 一样,优先用 Python 官方启动器 py,再用 PATH 里的 python
where py >nul 2>nul
if not errorlevel 1 goto usepy

where python >nul 2>nul
if not errorlevel 1 goto usepython

goto nopython

:usepy
set "PY=py -3"
goto pack

:usepython
set "PY=python"
goto pack

:pack
echo   正在打包,首次运行会慢一些…
echo.
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name UPnPtweaker main.py
if errorlevel 1 goto fail

echo.
echo   打包完成:dist\UPnPtweaker.exe
echo.
pause
exit /b 0

:nopython
echo.
echo   没找到可用的 Python,请先安装 Python 3.10 或更高版本。
echo   下载地址: https://www.python.org/downloads/
echo   安装时记得勾选 "Add Python to PATH"。
echo.
pause
exit /b 1

:fail
echo.
echo   打包失败,请查看上面的报错信息。
echo   如果提示找不到 PyInstaller,先执行: python -m pip install pyinstaller
echo.
pause
exit /b 1
