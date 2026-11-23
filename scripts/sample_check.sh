#!/bin/sh
# 固定检查脚本：在待测提交的 worktree 中执行。
# 退出码约定：0=通过，1=失败，125=不可测，其他退出码会让本轮中止。
if [ ! -f calc.py ]; then
    echo "calc.py 不存在（该提交中模块被改名），无法构建/测试"
    exit 125
fi
if [ ! -f test_calc.py ]; then
    echo "test_calc.py 不存在，无法测试"
    exit 125
fi
exec python3 test_calc.py
