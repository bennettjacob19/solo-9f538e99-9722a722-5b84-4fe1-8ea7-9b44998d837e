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
check("power", calc.power(2, 10), 1024)

if failures:
    print("FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ok")
sys.exit(0)
