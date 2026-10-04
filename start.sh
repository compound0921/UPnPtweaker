#!/usr/bin/env bash
# Linux / macOS 启动脚本
cd "$(dirname "$0")" || exit 1

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        # 确认版本 >= 3.8(能用 dataclasses 和延迟注解)
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "没有找到 Python 3.8 或更高版本,请先安装。" >&2
    exit 1
fi

# 图形界面需要 tkinter。Debian/Ubuntu 上要单独装:apt install python3-tk
if ! "$PY" -c 'import tkinter' 2>/dev/null; then
    echo "当前 Python 缺少 tkinter,无法打开图形界面。" >&2
    echo "  Debian/Ubuntu : sudo apt install python3-tk" >&2
    echo "  Fedora        : sudo dnf install python3-tkinter" >&2
    echo "  macOS(Homebrew): brew install python-tk" >&2
    echo >&2
    echo "也可以用命令行模式,它不需要 tkinter:" >&2
    echo "  $PY main.py list" >&2
    exit 1
fi

exec "$PY" main.py
