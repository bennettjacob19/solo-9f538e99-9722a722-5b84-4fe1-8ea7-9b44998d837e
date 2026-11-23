"""Tiny calculator module."""


def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        raise ValueError("cannot divide by zero")
    return a / b


def factorial(n):
    if n < 0:
        raise ValueError("negative input")
    result = 1
    for i in range(1, n):
        result *= i
    return result


def power(a, b):
    return a ** b


if __name__ == "__main__":
    import sys

    print(factorial(int(sys.argv[1])) if len(sys.argv) > 1 else "usage: calc.py N")
