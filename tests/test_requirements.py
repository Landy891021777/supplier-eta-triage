# -*- coding: utf-8 -*-
"""
requirements.txt 要列出程式真正用到的所有第三方套件。

當初的 bug（2026-09-26，雲端供應商績效頁 ModuleNotFoundError: openpyxl）：
匯出 Excel 用 `pd.ExcelWriter(buf, engine="openpyxl")`，openpyxl 是 pandas
以字串載入的，程式裡沒有 `import openpyxl`，requirements.txt 就漏列了。
本機早就裝過所以測試全過；雲端只照 requirements.txt 安裝，一打開頁面
（st.download_button 在畫面繪製時就會先產生檔案）整頁報錯。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("src", "views", "scripts")
CODE_FILES = ("app.py", "ui_state.py")

# import 名稱 → pip 套件名稱（不同名的才需要列）
IMPORT_TO_PIP = {"yaml": "PyYAML", "sklearn": "scikit-learn"}


def _requirements() -> set[str]:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(re.split(r"[<>=!~\[ ]", line)[0].lower())
    return names


def _py_files():
    for d in CODE_DIRS:
        yield from (ROOT / d).rglob("*.py")
    for f in CODE_FILES:
        yield ROOT / f


def _local_modules() -> set[str]:
    local = {p.stem for p in (ROOT / "src").rglob("*.py")}
    local |= {p.name for p in (ROOT / "src").iterdir() if p.is_dir()}
    local |= {"views", "ui_state", "app"}
    return local


def test_direct_imports_are_listed():
    std, local = set(sys.stdlib_module_names), _local_modules()
    listed = _requirements()
    missing = set()
    for p in _py_files():
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                mods = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module.split(".")[0]]
            else:
                continue
            for m in mods:
                if m in std or m in local:
                    continue
                if IMPORT_TO_PIP.get(m, m).lower() not in listed:
                    missing.add(m)
    assert not missing, f"程式 import 了但 requirements.txt 沒列：{sorted(missing)}"


def test_pandas_engines_are_listed():
    """pandas 以字串載入的引擎（engine="openpyxl"）不會出現在 import 裡，要另外掃。"""
    listed = _requirements()
    engines = set()
    for p in _py_files():
        engines |= set(re.findall(r'engine\s*=\s*["\'](\w+)["\']',
                                  p.read_text(encoding="utf-8")))
    missing = {e for e in engines if e.lower() not in listed}
    assert not missing, f"程式用到 pandas 引擎但 requirements.txt 沒列：{sorted(missing)}"


def test_requirements_file_is_ascii():
    """
    requirements.txt 只能有 ASCII。

    當初的 bug（2026-09-26）：檔案裡有中文註解，Windows 繁中環境的 pip
    用 cp950 讀檔，`pip install -r requirements.txt` 直接報 UnicodeDecodeError，
    README 快速開始的第一行指令就卡住。雲端是 Linux（UTF-8）讀得了，
    所以只有本機 Windows 使用者會遇到。
    """
    raw = (ROOT / "requirements.txt").read_bytes()
    bad = [i for i, b in enumerate(raw) if b > 127]
    assert not bad, f"requirements.txt 含非 ASCII 字元（第一個在位元組 {bad[0]}）"
