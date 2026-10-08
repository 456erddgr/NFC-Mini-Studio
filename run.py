# -*- coding: utf-8 -*-
"""
NFC Mini Studio · 启动入口
==========================
开发运行：    python run.py
调试模式：    python run.py --debug
"""
import io
import os
import sys

# windowed（无控制台）模式下 sys.stdout / stderr 为 None，
# 部分第三方库（bleak / pywebview）内部会 print，需要兜底，否则直接崩溃。
if sys.stdout is None:
    sys.stdout = io.TextIOWrapper(open(os.devnull, "wb"), write_through=True)
if sys.stderr is None:
    sys.stderr = io.TextIOWrapper(open(os.devnull, "wb"), write_through=True)

# 让 `python run.py` 与 PyInstaller 打包后都能 import app 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 自检模式：不开窗，跑完链路检查后直接退出（打包后也能用）
if "--selftest" in sys.argv:
    from app.selftest import run as selftest_run  # noqa: E402
    raise SystemExit(selftest_run())

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
