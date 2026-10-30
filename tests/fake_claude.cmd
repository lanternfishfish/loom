@echo off
REM 模拟 npm 装的 claude.cmd：一个批处理壳，里面再起真进程。
REM 用来验证 loom.py 对 .cmd 的处理（CreateProcess 不能直接执行批处理）。
python "%~dp0fake_claude.py" %*
