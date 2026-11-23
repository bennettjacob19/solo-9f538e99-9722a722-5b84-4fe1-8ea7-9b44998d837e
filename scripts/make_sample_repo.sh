#!/usr/bin/env bash
# 生成一个带线性历史的本地样例仓库（提交时间/作者固定，SHA 可复现）。
# 用法: make_sample_repo.sh <目标路径>
#
# 历史共 9 个提交（旧 -> 新）：
#   1 Add calculator and tests        通过
#   2 Add power function              通过
#   3 Improve division error message  通过
#   4 Refactor factorial loop         失败 ← 首个失败提交（off-by-one）
#   5 Add README                      失败
#   6 Rename module (WIP)             不可测（calc.py 被改名，脚本应退出 125）
#   7 Restore module name             失败
#   8 Add CLI interface               失败
#   9 Bump version                    失败
set -euo pipefail

REPO="${1:?用法: make_sample_repo.sh <目标路径>}"
mkdir -p "$REPO"
cd "$REPO"

if [ -d .git ]; then
  echo "仓库已存在: $REPO" >&2
  exit 1
fi

git init -q -b main
export GIT_AUTHOR_NAME="Demo Author" GIT_AUTHOR_EMAIL="demo@example.com"
export GIT_COMMITTER_NAME="Demo Author" GIT_COMMITTER_EMAIL="demo@example.com"
BASE=1704067200  # 2024-01-01T00:00:00Z
STEP=0

commit() {
  STEP=$((STEP + 1))
  export GIT_AUTHOR_DATE="@$((BASE + STEP * 60)) +0000"
  export GIT_COMMITTER_DATE="@$((BASE + STEP * 60)) +0000"
  git add -A
  git commit -q -m "$1"
}

# --- 提交 1: 计算器 + 测试 ---------------------------------------------
cat > calc.py <<'EOF'
"""Tiny calculator module."""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b


def factorial(n):
    if n < 0:
        raise ValueError("negative input")
    result = 1
    for i in range(1, n + 1):
        result *= i
    return result
EOF

cat > test_calc.py <<'EOF'
"""固定的验收测试：全部通过退出 0，否则退出 1。"""
import sys

import calc

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


check("add", calc.add(2, 3), 5)
check("sub", calc.sub(10, 4), 6)
check("mul", calc.mul(3, 4), 12)
check("div", calc.div(10, 4), 2.5)
check("factorial(1)", calc.factorial(1), 1)
check("factorial(3)", calc.factorial(3), 6)
check("factorial(5)", calc.factorial(5), 120)

if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ok")
sys.exit(0)
EOF
commit "Add calculator and tests"

# --- 提交 2: power 函数 -------------------------------------------------
cat >> calc.py <<'EOF'


def power(a, b):
    return a ** b
EOF
python3 - <<'EOF'
import re
src = open("test_calc.py").read()
src = src.replace('check("factorial(5)", calc.factorial(5), 120)',
                  'check("factorial(5)", calc.factorial(5), 120)\ncheck("power", calc.power(2, 10), 1024)')
open("test_calc.py", "w").write(src)
EOF
commit "Add power function"

# --- 提交 3: 改错误信息（仍然通过） -------------------------------------
sed -i 's/division by zero/cannot divide by zero/' calc.py
commit "Improve division error message"

# --- 提交 4: 引入 bug（factorial 差一） ---------------------------------
sed -i 's/range(1, n + 1)/range(1, n)/' calc.py
commit "Refactor factorial loop"

# --- 提交 5: 加 README --------------------------------------------------
cat > README.md <<'EOF'
# calc

A tiny calculator module used to demo the bisect tool.
EOF
commit "Add README"

# --- 提交 6: 改名（该提交不可测） ---------------------------------------
git mv calc.py calculator.py
commit "Rename module (WIP)"

# --- 提交 7: 改回来 -----------------------------------------------------
git mv calculator.py calc.py
commit "Restore module name"

# --- 提交 8: CLI --------------------------------------------------------
cat >> calc.py <<'EOF'


if __name__ == "__main__":
    import sys

    print(factorial(int(sys.argv[1])) if len(sys.argv) > 1 else "usage: calc.py N")
EOF
commit "Add CLI interface"

# --- 提交 9: 版本号 -----------------------------------------------------
cat > version.txt <<'EOF'
1.1
EOF
commit "Bump version"

echo "样例仓库已生成: $REPO"
git log --oneline --reverse
