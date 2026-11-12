"""带看门狗的 pytest 启动器：卡住时打印所有线程栈并退出。

用途：定位「CPU 空转/等待但 pytest 超时插件打不断」的挂死——那种情况通常是
主线程卡在 C 级调用里，Python 层的 `--timeout` 收不到信号，而 faulthandler 的
看门狗线程是独立的，能直接把每个线程的栈打出来。

用法（在容器内）：
    python scripts/fault_pytest.py tests/test_transfer.py::some_test -vv

环境变量 FAULT_AFTER 控制看门狗秒数，默认 90。
"""
import faulthandler
import os
import sys

import pytest

delay = int(os.environ.get('FAULT_AFTER', '90'))
dump_path = os.environ.get('FAULT_FILE', '/tmp/fault_stack.log')
# 显式写文件：dump 走 stderr 时，容器/重定向链路一旦丢流就白等了。
stream = open(dump_path, 'a', buffering=1)
# exit=True：打完栈直接退出，避免继续挂住占着容器。
faulthandler.dump_traceback_later(delay, exit=True, file=stream)
sys.exit(pytest.main(sys.argv[1:]))
