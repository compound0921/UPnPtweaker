@echo off
rem 双击启动图形界面。用 pyw / pythonw 启动,不残留控制台窗口。
cd /d "%~dp0"

rem 优先用 Python 官方启动器 pyw,并用 -3 明确指定 Python 3。
rem PATH 里如果有 Microsoft Store 的 python.exe 占位程序,直接调 pythonw 可能启动到假货。
where pyw >nul 2>nul
if not errorlevel 1 goto usepyw

where pythonw >nul 2>nul
if not errorlevel 1 goto usepythonw

goto nopython

:usepyw
start "" pyw -3 "%~dp0main.py"
exit /b 0

:usepythonw
start "" pythonw "%~dp0main.py"
exit /b 0

:nopython
echo.
echo   没有找到可用的 Python,请先安装 Python 3.10 或更高版本。
echo   下载地址: https://www.python.org/downloads/
echo   安装时记得勾选 "Add Python to PATH"。
echo.
echo   如果已经装好了,也可以在命令行里运行 python main.py 来查看具体报错。
echo.
pause
exit /b 1
