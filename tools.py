"""
NEMESIS tools (v7): exact maths, typing / dictation, Word documents, screenshots, reading text (OCR).

Everything here runs on the PC. nemesis.py calls  tools.init(...)  once and then
tools.skill(text)  for every sentence. skill() returns a reply, tools.SILENT
(did it, say nothing) or None (not mine).
"""
import ctypes
import datetime
import io
import math
import os
import re
import shutil
import subprocess
import threading
import time
from fractions import Fraction

import ast

SILENT = "\x00silent"
CTX = {"say": None, "hub": None, "generate": None, "base_dir": ".", "lang": lambda: "en",
       "skill": None, "last_reply": lambda: ""}
IS_WIN = os.name == "nt"


def init(**kw):
    CTX.update(kw)


def _say(text, target=None):
    if CTX["say"]:
        CTX["say"](text, target) if target else CTX["say"](text)


# =========================================================== number words
_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SCALES = {"thousand": 10 ** 3, "lakh": 10 ** 5, "lakhs": 10 ** 5, "lac": 10 ** 5, "million": 10 ** 6,
           "crore": 10 ** 7, "crores": 10 ** 7, "billion": 10 ** 9}
_NUMW = set(_UNITS) | set(_TENS) | set(_SCALES) | {"hundred"}


def _parse_number(toks, i):
    """Read one number phrase starting at toks[i]. Returns (value as str, next index)."""
    total, cur, j, seen = 0, 0, i, False
    while j < len(toks):
        w = toks[j]
        if w in _UNITS:
            cur += _UNITS[w]
        elif w in _TENS:
            cur += _TENS[w]
        elif w == "hundred":
            cur = (cur or 1) * 100
        elif w in _SCALES:
            total += (cur or 1) * _SCALES[w]
            cur = 0
        elif w == "and" and seen and j + 1 < len(toks) and toks[j + 1] in _NUMW and toks[j + 1] != "zero":
            pass
        else:
            break
        seen = True
        j += 1
    val = str(total + cur)
    if j < len(toks) and toks[j] == "point" and j + 1 < len(toks) and (toks[j + 1] in _UNITS or toks[j + 1].isdigit()):
        digits, k = "", j + 1
        while k < len(toks) and (toks[k] in _UNITS and _UNITS[toks[k]] < 10 or re.fullmatch(r"\d", toks[k])):
            digits += str(_UNITS.get(toks[k], toks[k]))
            k += 1
        if digits:
            val, j = val + "." + digits, k
    return val, j


def words_to_numbers(s):
    """'twenty five plus two point five' -> '25 plus 2.5'; '5 lakh' -> '500000'."""
    toks = re.findall(r"\d+(?:\.\d+)?|[a-z']+|[^\sa-z\d]", s.lower())
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"\d+(?:\.\d+)?", t) and i + 1 < len(toks) and toks[i + 1] in _SCALES:
            out.append(str(Fraction(t) * _SCALES[toks[i + 1]]) if "." in t else str(int(t) * _SCALES[toks[i + 1]]))
            i += 2
        elif t in _NUMW:
            val, j = _parse_number(toks, i)
            out.append(val)
            i = j
        else:
            out.append(t)
            i += 1
    return " ".join(out)


def _fix_homophones(s):
    """'5 plus to' -> '5 plus 2' (only next to an operator, never in normal sentences)."""
    ops = r"(?:plus|minus|times|into|multiplied by|divided by|over|\+|-|\*|/)"
    for word, digit in (("to", "2"), ("too", "2"), ("for", "4"), ("ate", "8"), ("won", "1")):
        s = re.sub(rf"({ops}) {word}\b", rf"\1 {digit}", s)
        s = re.sub(rf"\b{word} ({ops})", rf"{digit} \1", s)
    return s


# =========================================================== exact evaluator
def _num(node_value):
    if isinstance(node_value, bool):
        raise ValueError("bad")
    if isinstance(node_value, int):
        return Fraction(node_value)
    return Fraction(repr(node_value))


_DEG = math.pi / 180


def _fn(name, args):
    a = [float(x) for x in args]
    if name == "sqrt":
        if a[0] < 0:
            raise ValueError("negative root")
        r = math.isqrt(int(a[0])) if a[0] == int(a[0]) and int(a[0]) ** 0.5 == math.isqrt(int(a[0])) else math.sqrt(a[0])
        return Fraction(r) if isinstance(r, int) else r
    if name == "cbrt":
        r = round(abs(a[0]) ** (1 / 3))
        if r ** 3 == abs(a[0]):
            return Fraction(r if a[0] >= 0 else -r)
        return math.copysign(abs(a[0]) ** (1 / 3), a[0])
    if name == "log":
        return math.log10(a[0])
    if name == "ln":
        return math.log(a[0])
    if name in ("sin", "cos", "tan"):
        v = getattr(math, name)(a[0] * _DEG)
        return 0.0 if abs(v) < 1e-12 else (float("inf") if abs(v) > 1e12 else round(v, 12))
    if name == "fact":
        if a[0] < 0 or a[0] != int(a[0]) or a[0] > 500:
            raise ValueError("factorial")
        return Fraction(math.factorial(int(a[0])))
    if name == "abs":
        return abs(args[0])
    raise ValueError("function")


_CONST = {"pi": math.pi, "e": math.e}


def evaluate(expr, variables=None):
    """Exact arithmetic with fractions. Raises ValueError on anything unsafe."""
    variables = variables or {}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return _num(n.value)
        if isinstance(n, ast.Name):
            if n.id in variables:
                return variables[n.id]
            if n.id in _CONST:
                return _CONST[n.id]
            raise ValueError("name")
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
            if isinstance(n.op, ast.Mod):
                return a % b
            if isinstance(n.op, ast.FloorDiv):
                return a // b
            if isinstance(n.op, ast.Pow):
                if isinstance(b, Fraction) and b.denominator == 1 and abs(b) <= 1000 and \
                        (not isinstance(a, Fraction) or abs(a) < 10 ** 6 or abs(b) <= 20):
                    return a ** int(b)
                fa, fb = float(a), float(b)
                if abs(fb) > 1000 or (fa < 0 and fb != int(fb)):
                    raise ValueError("power")
                return fa ** fb
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in (
                "sqrt", "cbrt", "log", "ln", "sin", "cos", "tan", "fact", "abs") and len(n.args) == 1:
            return _fn(n.func.id, [ev(n.args[0])])
        raise ValueError("not allowed")

    return ev(ast.parse(expr.strip(), mode="eval"))


def fmt(v):
    """Number -> short spoken-friendly text."""
    if isinstance(v, Fraction):
        if v.denominator == 1:
            return str(v.numerator)
        v = float(v)
    if v != v or v in (float("inf"), float("-inf")):
        return "undefined"
    if abs(v) >= 1e15:
        return f"{v:.6e}"
    s = f"{v:.8f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# =========================================================== maths skill
_PRE = r"^(?:please )?(?:what(?:'s| is)|calculate|compute|how much is|solve|work out|find|tell me)\s+(?:the )?"


def _nums(s):
    return [Fraction(x) for x in re.findall(r"-?\d+(?:\.\d+)?", s)]


def _clean_for_math(t):
    s = t.lower().replace(",", " , ")
    s = re.sub(r"(?<=\d) , (?=\d{3}\b)", "", s)          # 1,000 -> 1000
    s = s.replace(" , ", ",")
    s = words_to_numbers(s)
    return _fix_homophones(s)


def _money(v):
    f = float(v)
    return f"{f:,.2f}".rstrip("0").rstrip(".") if f != int(f) else f"{int(f)}"


def special_maths(t, s):
    """Percent, GST, discount, EMI, interest, average and equation problems."""
    m = re.search(r"(\d+(?:\.\d+)?) is what (?:percent|percentage|%) of (\d+(?:\.\d+)?)", s)
    m2 = None if m else re.search(r"what (?:percent|percentage|%) of (\d+(?:\.\d+)?) is (\d+(?:\.\d+)?)", s)
    if m or m2:
        a, b = (Fraction((m or m2).group(1)), Fraction((m or m2).group(2)))
        if m2:
            a, b = b, a
        return None if b == 0 else f"{fmt(a)} is {fmt(a / b * 100)} percent of {fmt(b)}."
    m = re.search(r"(?:percent|percentage)(?: change| increase| decrease| difference)? (?:from|between) (\d+(?:\.\d+)?) (?:to|and) (\d+(?:\.\d+)?)", s)
    if m:
        a, b = Fraction(m.group(1)), Fraction(m.group(2))
        if a == 0:
            return None
        ch = (b - a) / a * 100
        return f"That is {'an increase' if ch >= 0 else 'a decrease'} of {fmt(abs(ch))} percent."
    m = re.search(r"(increase|decrease|reduce|raise|cut) (\d+(?:\.\d+)?) by (\d+(?:\.\d+)?) (?:percent|%)", s)
    if m:
        a, p = Fraction(m.group(2)), Fraction(m.group(3))
        up = m.group(1) in ("increase", "raise")
        return f"{fmt(a)} {'increased' if up else 'decreased'} by {fmt(p)} percent is {fmt(a * (1 + p / 100 if up else 1 - p / 100))}."
    m = re.search(r"(add|plus|with|including) (\d+(?:\.\d+)?) (?:percent|%) (?:gst|tax|vat) (?:to|on) (\d+(?:\.\d+)?)", s) or \
        re.search(r"(?:gst|tax|vat) (?:on|of) (\d+(?:\.\d+)?) (?:at|of) (\d+(?:\.\d+)?) (?:percent|%)", s)
    if m:
        g = m.groups()
        rate, amt = (Fraction(g[1]), Fraction(g[2])) if g[0] in ("add", "plus", "with", "including") else (Fraction(g[1]), Fraction(g[0]))
        tax = amt * rate / 100
        return f"Tax is {_money(tax)}. Total with tax is {_money(amt + tax)}."
    m = re.search(r"(?:remove|without|excluding|before) (\d+(?:\.\d+)?) (?:percent|%) (?:gst|tax|vat) (?:from|on) (\d+(?:\.\d+)?)", s)
    if m:
        rate, total = Fraction(m.group(1)), Fraction(m.group(2))
        base = total / (1 + rate / 100)
        return f"The price before tax is {_money(base)}. The tax part is {_money(total - base)}."
    m = re.search(r"(\d+(?:\.\d+)?) (?:percent|%) (?:discount|off) (?:on|of|from) (\d+(?:\.\d+)?)", s) or \
        re.search(r"discount of (\d+(?:\.\d+)?) (?:percent|%) (?:on|of|from) (\d+(?:\.\d+)?)", s)
    if m:
        p, a = Fraction(m.group(1)), Fraction(m.group(2))
        return f"The discount is {_money(a * p / 100)}. You pay {_money(a - a * p / 100)}."
    if re.search(r"\bemi\b", s):
        n = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s)]
        if len(n) >= 3:
            p, rate, period = n[0], n[1], n[2]
            months = period * 12 if re.search(r"\byears?\b|\byr", s) else period
            r = rate / 12 / 100
            emi = p / months if r == 0 else p * r * (1 + r) ** months / ((1 + r) ** months - 1)
            total = emi * months
            return (f"Your EMI is about {emi:,.0f} per month. Total payment is {total:,.0f}, "
                    f"of which interest is {total - p:,.0f}.")
        return "For an EMI I need the loan amount, the yearly interest rate and the time. For example: EMI for 500000 at 9 percent for 5 years."
    m = re.search(r"(simple|compound) interest .*?(\d+(?:\.\d+)?) .*?(\d+(?:\.\d+)?) (?:percent|%).*?(\d+(?:\.\d+)?) (year|month)", s)
    if m:
        kind, p, rate, per, unit = m.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4)), m.group(5)
        years = per if unit == "year" else per / 12
        interest = p * rate * years / 100 if kind == "simple" else p * ((1 + rate / 100) ** years - 1)
        return f"The {kind} interest is {interest:,.2f}. The total amount is {p + interest:,.2f}."
    m = re.search(r"\b(average|mean|sum|total|maximum|minimum|largest|smallest|biggest) (?:of )?(.+)", s)
    if m and len(re.findall(r"\d", m.group(2))) >= 2:
        vals = _nums(m.group(2))
        if len(vals) >= 2:
            w = m.group(1)
            if w in ("average", "mean"):
                return f"The average is {fmt(sum(vals) / len(vals))}."
            if w in ("sum", "total"):
                return f"The sum is {fmt(sum(vals))}."
            if w in ("maximum", "largest", "biggest"):
                return f"The largest is {fmt(max(vals))}."
            return f"The smallest is {fmt(min(vals))}."
    if re.search(r"\bsolve\b|\bfind x\b|=|\bequals\b", t) and re.search(r"\bx\b|\d\s*x\b", s):
        return solve_equation(s)
    return None


def solve_equation(s):
    s = re.sub(r"^(?:please )?(?:solve|find x in|find x for|what is x in)\s*", "", s).strip(" ?.")
    s = s.replace("equals", "=").replace("is equal to", "=").replace("equal to", "=")
    s = re.sub(r"\bx squared\b", "x**2", s)
    s = re.sub(r"\bx cubed\b", "x**3", s)
    for pat, rep in ((r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\b(?:times|multiplied by)\b", "*"),
                     (r"\b(?:divided by|over)\b", "/"), (r"\^", "**")):
        s = re.sub(pat, rep, s)
    s = re.sub(r"(\d)\s*x\b", r"\1*x", s)
    s = re.sub(r"\)\s*x\b", ")*x", s)
    s = re.sub(r"\bx\s*(?=\()", "x*", s)
    if s.count("=") != 1 or not re.fullmatch(r"[\dx+\-*/().=\s]+", s):
        return None
    left, right = s.split("=")
    try:
        def f(v):
            return evaluate(left, {"x": Fraction(v)}) - evaluate(right, {"x": Fraction(v)})
        f0, f1, f2, f3 = f(0), f(1), f(2), f(3)
    except Exception:
        return None
    a = (f2 - 2 * f1 + f0) / 2
    b = f1 - f0 - a
    c = f0
    if a * 9 + b * 3 + c != f3:
        return "I can only solve linear and quadratic equations."
    if a == 0:
        if b == 0:
            return "Every value of x works." if c == 0 else "There is no solution."
        return f"x equals {fmt(-c / b)}."
    disc = b * b - 4 * a * c
    if disc < 0:
        return "There is no real solution."
    d = float(disc)
    root = math.isqrt(int(disc)) if disc.denominator == 1 and math.isqrt(int(disc)) ** 2 == disc else None
    if root is not None or (disc.denominator != 1 and math.isqrt(disc.numerator) ** 2 == disc.numerator and
                            math.isqrt(disc.denominator) ** 2 == disc.denominator):
        sq = Fraction(root) if root is not None else Fraction(math.isqrt(disc.numerator), math.isqrt(disc.denominator))
        x1, x2 = (-b + sq) / (2 * a), (-b - sq) / (2 * a)
    else:
        x1, x2 = (-float(b) + math.sqrt(d)) / (2 * float(a)), (-float(b) - math.sqrt(d)) / (2 * float(a))
    if x1 == x2:
        return f"x equals {fmt(x1)}."
    lo, hi = sorted((x1, x2))
    return f"x equals {fmt(lo)} or {fmt(hi)}."


_OPS = [(r"\bplus\b|\band\b(?= \d)", "+"), (r"\bminus\b|\bless\b", "-"),
        (r"\b(?:times|multiplied by|multiply by|into|cross)\b", "*"),
        (r"\b(?:divided by|divide by|over|upon)\b", "/"), (r"\bto the power (?:of )?|\braised to\b", "**"),
        (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"), (r"\bmod(?:ulo)?\b|\bremainder\b", "%"),
        (r"(?<=\d)\s*x\s*(?=\d)", "*"), (r"\^", "**"), (r"\bpi\b", "pi")]


def math_skill(t):
    """Spoken or typed arithmetic -> '25 times 4 equals 100.'  None when not maths."""
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t.strip(" .?!").lower())
    if not re.search(r"\d|\b(?:" + "|".join(_NUMW) + r")\b", t):
        return None
    if re.search(r"\b(timer|alarm|remind|minutes?|hours?|seconds?|o'?clock|am|pm|volume|percent volume)\b", t) and \
            not re.search(r"\b(plus|minus|times|divided|emi|interest|gst|discount)\b", t):
        return None
    s = _clean_for_math(t)
    special = special_maths(t, s)
    if special:
        return special
    e = re.sub(_PRE, "", s)
    e = re.sub(r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+", r"(\1/100)*", e)
    e = re.sub(r"\bsquare root of\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", e)
    e = re.sub(r"\bcube root of\s*(\d+(?:\.\d+)?)", r"cbrt(\1)", e)
    e = re.sub(r"\b(?:square of)\s*(\d+(?:\.\d+)?)", r"(\1)**2", e)
    e = re.sub(r"\b(?:cube of)\s*(\d+(?:\.\d+)?)", r"(\1)**3", e)
    e = re.sub(r"(\d+(?:\.\d+)?)\s*factorial", r"fact(\1)", e)
    e = re.sub(r"(\d+)\s*!", r"fact(\1)", e)
    e = re.sub(r"\b(?:log|logarithm)(?: base 10)?(?: of)?\s*(\d+(?:\.\d+)?)", r"log(\1)", e)
    e = re.sub(r"\b(?:ln|natural log)(?: of)?\s*(\d+(?:\.\d+)?)", r"ln(\1)", e)
    e = re.sub(r"\b(sin|cos|tan)(?:e)?(?: of)?\s*(\d+(?:\.\d+)?)(?: degrees?)?", r"\1(\2)", e)
    for pat, rep in _OPS:
        e = re.sub(pat, rep, e)
    e = e.replace("=", "").strip(" ?.")
    if not re.search(r"\d|pi", e) or not re.search(r"[-+*/%]|sqrt|cbrt|fact|log|ln|sin|cos|tan", e):
        return None
    if not re.fullmatch(r"(?:sqrt|cbrt|fact|log|ln|sin|cos|tan|pi|[\d\s+\-*/().%])+", e):
        return None
    try:
        v = evaluate(e)
    except ZeroDivisionError:
        return "I cannot divide by zero."
    except Exception:
        return None
    say_expr = re.sub(_PRE, "", _fix_homophones(words_to_numbers(t))).strip(" ?.=")
    say_expr = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", say_expr))
    for sym, word in ((r"\s\*\s", " times "), (r"\s/\s", " divided by "), (r"\s\+\s", " plus "),
                      (r"\s-\s", " minus "), (r"\s\^\s", " to the power of "), (r"\s%\s", " mod ")):
        say_expr = re.sub(sym, word, say_expr)
    return f"{say_expr} equals {fmt(v)}."


# =========================================================== windows helpers
def foreground_title():
    if not IS_WIN:
        return ""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


GUARD_RE = re.compile(r"powershell|command prompt|cmd\.exe|windows terminal|\bterminal\b|git bash|\bwsl\b|ubuntu|"
                      r"^run$|registry editor|user account control|windows security|task manager|^select .*prompt", re.I)


def typing_blocked():
    """Refuse to type into a command window: a stranger with the phone link must never run commands."""
    title = foreground_title()
    if GUARD_RE.search(title):
        return f"I will not type into {title or 'a command window'}. Click a normal window like Notepad first."
    return None


VK = {"backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "escape": 0x1B,
      "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
      "right": 0x27, "down": 0x28, "delete": 0x2E, "win": 0x5B, "f4": 0x73, "f5": 0x74}
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK[_c] = ord(_c.upper())


class _KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _HI(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]


class _IU(ctypes.Union):
    _fields_ = [("ki", _KI), ("mi", _MI), ("hi", _HI)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _IU)]


def _send(vk=0, scan=0, flags=0):
    extra = ctypes.c_ulong(0)
    inp = _INPUT(1, _IU(ki=_KI(vk, scan, flags, 0, ctypes.pointer(extra))))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _type_unit(code):
    _send(0, code, 0x0004)
    _send(0, code, 0x0004 | 0x0002)


def press(*keys):
    """press('ctrl','a') holds ctrl, taps a."""
    codes = [VK[k] for k in keys]
    for c in codes[:-1]:
        _send(c)
    _send(codes[-1])
    _send(codes[-1], 0, 0x0002)
    for c in reversed(codes[:-1]):
        _send(c, 0, 0x0002)
    time.sleep(0.03)


def type_text(text, delay=0.008):
    for ch in text:
        if ch == "\n":
            press("enter")
        elif ch == "\t":
            press("tab")
        else:
            data = ch.encode("utf-16-le")
            for k in range(0, len(data), 2):
                _type_unit(int.from_bytes(data[k:k + 2], "little"))
        time.sleep(delay)


# =========================================================== keyboard commands
KEY_COMMANDS = [
    (r"(?:press |hit )?(?:enter|return)(?: key)?|new line|next line", ("enter",)),
    (r"new paragraph", ("enter", "enter")),
    (r"press tab|tab key", ("tab",)),
    (r"press (?:escape|esc)", ("escape",)),
    (r"press space(?:bar)?", ("space",)),
    (r"press backspace|backspace", ("backspace",)),
    (r"press delete|delete key", ("delete",)),
    (r"press (?:the )?(up|down|left|right)(?: arrow)?", None),
    (r"press page (up|down)", None),
    (r"delete (?:the )?last word", ("ctrl", "backspace")),
    (r"select all", ("ctrl", "a")),
    (r"copy(?: that| this| it| selection)?", ("ctrl", "c")),
    (r"cut(?: that| this| it)?", ("ctrl", "x")),
    (r"paste(?: that| it| here)?", ("ctrl", "v")),
    (r"undo(?: that)?", ("ctrl", "z")),
    (r"redo(?: that)?", ("ctrl", "y")),
    (r"save(?: it| this| the file| file| document)?", ("ctrl", "s")),
    (r"find(?: in page)?", ("ctrl", "f")),
    (r"refresh(?: the page)?|reload(?: the page)?", ("f5",)),
    (r"new tab", ("ctrl", "t")),
    (r"close (?:this )?tab", ("ctrl", "w")),
    (r"next tab", ("ctrl", "tab")),
    (r"close (?:this |the )?window", ("alt", "f4")),
    (r"(?:switch|next) window|alt tab", ("alt", "tab")),
    (r"show (?:the )?desktop|minimi[sz]e (?:all|everything)", ("win", "d")),
]
FREE_KEYS = {("alt", "tab"), ("win", "d")}


def run_keys(keys):
    if tuple(keys) not in FREE_KEYS:
        blocked = typing_blocked()
        if blocked:
            return blocked
    try:
        if tuple(keys) == ("enter", "enter"):
            press("enter")
            press("enter")
        else:
            press(*keys)
    except Exception as e:
        return f"I could not press the keys: {e}"
    return SILENT


def key_skill(t):
    for pat, keys in KEY_COMMANDS:
        m = re.fullmatch(pat, t)
        if m:
            if keys is None:
                keys = (("pageup" if m.group(1) == "up" else "pagedown") if "page" in pat else m.group(1),)
            return run_keys(keys)
    return None


# =========================================================== typing + dictation
DICT = {"on": False, "start": True, "last": 0}
PUNCT = [(r"\b(?:full stop|period)\b", "."), (r"\bcomma\b", ","), (r"\bquestion mark\b", "?"),
         (r"\bexclamation (?:mark|point)\b", "!"), (r"\bsemi ?colon\b", ";"), (r"\bcolon\b", ":"),
         (r"\b(?:open|left) (?:bracket|parenthesis)\b", "("), (r"\b(?:close|right) (?:bracket|parenthesis)\b", ")"),
         (r"\b(?:open )?quote\b|\b(?:close|end) quote\b", '"'), (r"\bapostrophe\b", "'"), (r"\bhyphen\b", "-"),
         (r"\bdash\b", " - "), (r"\bat sign\b", "@"), (r"\bslash\b", "/"), (r"\bnew paragraph\b", "\n\n"),
         (r"\b(?:new|next) line\b", "\n"), (r"पूर्ण विराम", "।"), (r"प्रश्न चिन्ह|प्रश्नचिन्ह", "?"), (r"अल्पविराम|कॉमा", ",")]


def format_dictation(text):
    s = text
    for pat, rep in PUNCT:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"\s+([,.;:?!।])", r"\1", s)
    s = re.sub(r"\s*\n\s*", lambda m: m.group(0).strip(" ") or "\n", s)
    s = s.strip(" ")
    if DICT["start"] and s:
        s = s[0].upper() + s[1:]
    s = re.sub(r"([.?!।]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)
    return s


def dictating():
    return DICT["on"]


def dictation_step(text):
    """One spoken sentence while dictation is on."""
    low = text.lower().strip(" .!?,")
    if re.fullmatch(r"(?:stop|end|finish|cancel|exit|turn off) (?:the )?(?:dictation|typing(?: mode)?)|dictation (?:off|stop|end)", low):
        DICT["on"] = False
        if CTX["hub"]:
            CTX["hub"].set_info(mode="")
        return "Dictation off."
    if re.fullmatch(r"(?:delete|scratch|remove|undo) that", low):
        n = DICT["last"]
        blocked = typing_blocked()
        if blocked:
            return blocked
        for _ in range(n):
            press("backspace")
        DICT["last"] = 0
        return SILENT
    for pat, keys in KEY_COMMANDS:
        if re.fullmatch(pat, low) and keys and pat.startswith(("select all", "undo", "redo", "save", "press", "(?:press |hit )", "new paragraph", "new line")):
            if "new" in pat:
                DICT["start"] = True
            return run_keys(keys)
    blocked = typing_blocked()
    if blocked:
        return blocked
    out = format_dictation(text)
    if not out:
        return SILENT
    try:
        type_text(out + ("" if out.endswith("\n") else " "))
    except Exception as e:
        return f"I could not type: {e}"
    DICT["last"] = len(out) + (0 if out.endswith("\n") else 1)
    DICT["start"] = bool(re.search(r"[.?!।\n]\s*$", out))
    if CTX["hub"]:
        CTX["hub"].log("sys", "typed: " + out.strip()[:80])
    return SILENT


def start_dictation():
    blocked = typing_blocked()
    if blocked:
        return blocked
    DICT.update(on=True, start=True, last=0)
    if CTX["hub"]:
        CTX["hub"].set_info(mode="DICTATION")
    return "Dictation on. Click where the text should go and start talking. Say stop dictation when you are done."


def typing_skill(t):
    if re.fullmatch(r"(?:start|begin|turn on|enable) (?:the )?(?:dictation|typing mode)|dictation(?: mode)?|take (?:a )?dictation|dictate", t):
        return start_dictation()
    m = re.match(r"(?:open|launch|start) ([\w ]+?) (?:and|then) (?:type|write) (.+)", t)
    if m and CTX["skill"]:
        reply = CTX["skill"]("open " + m.group(1))
        if reply and not reply.lower().startswith(("opening", "i could not find")):
            return reply
        _say(reply or "Opening.")
        time.sleep(2.5)
        return _type_now(m.group(2))
    m = re.match(r"(?:please )?type (?:this |out )?(?!of\b)(.+)", t)
    if m:
        return _type_now(m.group(1))
    return key_skill(t)


def _type_now(text):
    blocked = typing_blocked()
    if blocked:
        return blocked
    if len(text) > 2000:
        return "That is too long to type. I can write it into a document instead."
    try:
        type_text(text)
    except Exception as e:
        return f"I could not type: {e}"
    return SILENT


# =========================================================== documents
DOC_DIR = lambda: os.path.join(CTX["base_dir"], "documents")
KINDS = r"letter|essay|email|e-mail|application|report|summary|article|speech|paragraph|story|poem|resume|cv|notes|document|assignment|note|message|post|bio"


def _slug(s, n=40):
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return (s[:n] or "document")


def _plain(text):
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def save_docx(text, path, title=None):
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(12)
    if title:
        doc.add_heading(title, level=1)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if para:
            doc.add_paragraph(para)
    doc.save(path)


def save_text_as(text, name=None, kind="document", docx=True):
    os.makedirs(DOC_DIR(), exist_ok=True)
    base = _slug(name or kind) + "_" + datetime.datetime.now().strftime("%H%M%S")
    first = text.strip().split("\n", 1)[0]
    title = first if (kind in ("essay", "report", "article", "story", "speech", "poem") and len(first) < 80
                      and not first.endswith(".")) else None
    body = text.strip().split("\n", 1)[1] if title and "\n" in text.strip() else text
    if docx:
        try:
            path = os.path.join(DOC_DIR(), base + ".docx")
            save_docx(body, path, title)
            return path
        except ImportError:
            pass
    path = os.path.join(DOC_DIR(), base + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return path


def _open_file(path):
    try:
        os.startfile(path)
    except Exception:
        pass


def _job_write(kind, kind_full, rest, dest, name, lang):
    topic = (kind_full + (" " + rest if rest else ""))
    prompt = (f"Write a {kind_full}{(' ' + rest) if rest else ''}.\n"
              "Use plain text only: no markdown, no asterisks, no bullet symbols. "
              "Separate paragraphs with a blank line. ")
    if kind in ("letter", "application", "email", "e-mail", "message"):
        prompt += "Use a proper greeting and closing. Use [Your Name] where a name is needed. "
    else:
        prompt += "Start with a short title on the first line. "
    if lang == "hi":
        prompt += "Write in Hindi using Devanagari script. "
    elif lang == "mr":
        prompt += "Write in Marathi using Devanagari script. "
    try:
        text = _plain(CTX["generate"](prompt, 650))
    except Exception as e:
        _say(f"I could not write it. {e}")
        return
    if not text:
        _say("The brain gave me nothing. Please try again.")
        return
    if dest in ("notepad", "type"):
        title = foreground_title()
        if (dest == "notepad" and "notepad" not in title.lower()) or typing_blocked():
            path = save_text_as(text, name or topic, kind, docx=False)
            _say("I finished writing, but the right window is not in front, so I saved it as a text file in the documents folder.")
            return
        _say("Typing it now.")
        try:
            type_text(text)
        except Exception as e:
            _say(f"I could not type: {e}")
            return
        _say("Done typing.")
        return
    path = save_text_as(text, name or topic, kind, docx=True)
    ext = "Word file" if path.endswith(".docx") else "text file"
    _say(f"Your {kind} is ready. I saved it as a {ext} in the documents folder and I am opening it.")
    _open_file(path)


def document_skill(t):
    m = re.match(r"(?:please )?(?:save|store|put|export)\b (?:that|this|it|the (?:answer|reply|last (?:answer|reply)))\b.*?\b(?:as|in|to)\b.*?\b(word|docx?|document|text|txt|notepad) ?(?:file|document)?(?: called| named| as)? ?(.*)$", t)
    if m:
        text = CTX["last_reply"]()
        if not text:
            return "I have nothing to save yet."
        word = m.group(1) in ("word", "doc", "docx", "document")
        path = save_text_as(text, m.group(2).strip() or "saved_reply", "document", docx=word)
        _open_file(path)
        return f"Saved in the documents folder as {os.path.basename(path)}."
    if not re.match(r"(?:please )?(?:write|draft|create|make|prepare|compose|generate)\b", t):
        return None
    kind_m = re.search(rf"\b({KINDS})\b", t)
    word = bool(re.search(r"\b(?:word|docx?|document|doc file|save(?: it)?|file)\b", t))
    notepad = bool(re.search(r"\b(?:in|into|on|using) (?:the )?(?:notepad|note pad)\b", t))
    typeit = bool(re.search(r"\b(?:and type it|type it|and type|in the window|where i click)\b", t))
    if not kind_m or not (word or notepad or typeit):
        return None
    if CTX["generate"] is None:
        return "The brain is not ready."
    body = t
    name = None
    mm = re.search(r"\b(?:called|named|name it)\s+([\w ]{2,40}?)(?=\s+(?:in|on|about|and|using|with|regarding)\b|$)", body)
    if mm:
        name = mm.group(1).strip()
        body = body.replace(mm.group(0), " ")
    body = re.sub(r"\b(?:and\s+)?(?:save|store|export)\b.*$", "", body)
    body = re.sub(r"\b(?:and\s+)?type(?: it)?\b.*$", "", body)
    body = re.sub(r"\b(?:in|into|on|using) (?:the )?(?:notepad|note pad|ms word|microsoft word|word)\b", "", body)
    body = re.sub(r"\b(?:as|in) (?:a |an )?(?:word|document|docx?)(?: file)?\b", "", body)
    body = re.sub(r"^(?:please )?(?:write|draft|create|make|prepare|compose|generate)(?: me)?(?: an?| the)?\s*", "", body).strip(" ,.")
    km = re.search(rf"\b({KINDS})\b", body)
    if not km:
        return None
    kind = km.group(1)
    kind_full = body[:km.end()].strip()
    rest = body[km.end():].strip(" ,.")
    if len(kind_full.split()) > 4:
        kind_full = kind
    if not rest and kind == "document":
        return "What should the document be about?"
    dest = "notepad" if notepad else "type" if typeit else "word"
    if dest == "notepad" and CTX["skill"]:
        CTX["skill"]("open notepad")
    threading.Thread(target=_job_write, args=(kind, kind_full, rest, dest, name, CTX["lang"]()), daemon=True).start()
    return (f"Writing your {kind_full}{(' ' + rest) if rest else ''}. On this PC that can take a few minutes. "
            "I will tell you when it is ready.")


# =========================================================== screenshots + OCR
def _shot_dir():
    d = os.path.join(CTX["base_dir"], "screenshots")
    os.makedirs(d, exist_ok=True)
    return d


LAST_SHOT = {"path": None}


def grab_screen(window=False):
    from PIL import ImageGrab
    bbox = None
    if window and IS_WIN:
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
            r = RECT()
            u = ctypes.windll.user32
            if u.GetWindowRect(u.GetForegroundWindow(), ctypes.byref(r)) and r.r - r.l > 50:
                bbox = (max(0, r.l), max(0, r.t), r.r, r.b)
        except Exception:
            bbox = None
    return ImageGrab.grab(bbox=bbox, all_screens=False) if bbox else ImageGrab.grab()


def take_shot(window=False, delay=0):
    if delay:
        time.sleep(delay)
    img = grab_screen(window)
    path = os.path.join(_shot_dir(), datetime.datetime.now().strftime("shot_%Y%m%d_%H%M%S.png"))
    img.save(path)
    LAST_SHOT["path"] = path
    return path, img


def send_image_to_phone(img):
    hub = CTX["hub"]
    if not hub or not hasattr(hub, "add_image"):
        return False
    im = img.convert("RGB")
    if im.width > 1280:
        im = im.resize((1280, int(im.height * 1280 / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    hub.add_image(buf.getvalue())
    return True


def tesseract_cmd():
    found = shutil.which("tesseract")
    if found:
        return found
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Tesseract-OCR\tesseract.exe")):
        if os.path.exists(p):
            return p
    return None


OCR_HELP = ("To read text I need Tesseract. In PowerShell run: winget install UB-Mannheim.TesseractOCR "
            "and then: pip install pytesseract. Tick Hindi and Marathi in its installer if you want those languages.")


def ocr_image(img, prefer=None):
    """Returns (text, error_message)."""
    exe = tesseract_cmd()
    if not exe:
        return "", OCR_HELP
    try:
        import pytesseract
    except ImportError:
        return "", "I need the pytesseract package. Run: pip install pytesseract"
    pytesseract.pytesseract.tesseract_cmd = exe
    try:
        have = set(pytesseract.get_languages())
    except Exception:
        have = {"eng"}
    want = ["eng"]
    if prefer in ("hi", "mr"):
        want += [l for l in ("hin", "mar") if l in have]
    langs = "+".join(l for l in want if l in have) or "eng"
    g = img.convert("L")
    if g.width < 1200:
        g = g.resize((g.width * 2, g.height * 2))
    text = pytesseract.image_to_string(g, lang=langs)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), None


def to_clipboard(text):
    if not IS_WIN:
        return False
    try:
        subprocess.run(["clip"], input=text.encode("utf-16"), timeout=5)
        return True
    except Exception:
        return False


def read_screen(window=False):
    try:
        path, img = take_shot(window)
    except Exception as e:
        return f"I could not capture the screen: {e}"
    lang = CTX["lang"]()
    text, err = ocr_image(img, lang if lang != "en" else None)
    if err:
        return err
    if not text:
        return "I could not find any readable text on the screen."
    words = len(text.split())
    copied = to_clipboard(text)
    spoken = re.sub(r"\s+", " ", text)[:380]
    more = " The rest is " + ("copied to your clipboard." if copied else "in the screenshots folder.") if len(text) > 380 or copied else ""
    return f"I read {words} words. {spoken}{'...' if len(text) > 380 else ''}{more}"


def screenshot_skill(t):
    if re.search(r"\b(?:open|show) (?:the )?screenshots? (?:folder|directory)\b", t):
        try:
            os.startfile(_shot_dir())
            return "Opening the screenshots folder."
        except Exception:
            return f"The screenshots are in {_shot_dir()}."
    if re.search(r"\b(?:read|ocr|extract text from|what(?:'s| is) written on|what does .* say)\b.*\b(?:screen|window|page|display)\b|"
                 r"\bread (?:the |my )?(?:screen|window)\b|^ocr$|\bread (?:this|it) (?:out|aloud)\b", t):
        return read_screen(window="window" in t)
    if re.search(r"\b(?:show|send|display)\b.*\b(?:screenshot|screen shot|screen)\b.*\b(?:phone|here)\b|"
                 r"\b(?:show|send) me (?:the |a )?(?:screenshot|screen)\b|\bsend (?:the )?screenshot\b", t):
        try:
            if "new" in t or not LAST_SHOT["path"] or not os.path.exists(LAST_SHOT["path"]):
                path, img = take_shot("window" in t)
            else:
                from PIL import Image
                img = Image.open(LAST_SHOT["path"])
        except Exception as e:
            return f"I could not capture the screen: {e}"
        return "Sent the screenshot to your phone." if send_image_to_phone(img) else "I could not reach your phone."
    if re.search(r"\b(?:take|capture|grab|make)\b.*\b(?:screenshot|screen shot)\b|^screenshot\b", t):
        window = bool(re.search(r"\b(?:window|app|active|this)\b", t)) and "screen" not in t.replace("screenshot", "")
        m = re.search(r"\b(?:in|after) (\d{1,2}) seconds?\b", words_to_numbers(t))
        delay = int(m.group(1)) if m else 0
        try:
            if delay:
                _say(f"Taking it in {delay} seconds.")
            path, img = take_shot(window, delay)
        except ImportError:
            return "I need the pillow package for that. Run: pip install pillow"
        except Exception as e:
            return f"I could not take the screenshot: {e}"
        sent = " I also sent it to your phone." if re.search(r"\bphone\b", t) and send_image_to_phone(img) else ""
        return ("Window screenshot" if window else "Screenshot") + f" saved as {os.path.basename(path)} in the screenshots folder.{sent}"
    return None


# =========================================================== entry point
def skill(t):
    t = t.strip(" .?!,").lower() if t else ""
    if not t:
        return None
    if DICT["on"]:                       # (normally handled earlier in process())
        return dictation_step(t)
    for fn in (screenshot_skill, typing_skill, document_skill, math_skill):
        r = fn(t)
        if r:
            return r
    return None
