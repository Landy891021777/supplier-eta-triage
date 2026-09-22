# -*- coding: utf-8 -*-
"""
晶圓廠世界設定的一致性測試。

守住的是「世界不自相矛盾」：光阻不會去跟氣體廠買、每個料別都有收貨處理天數、
手寫信件裡的單號真的存在而且供應商對得上。這些錯了不會報錯，只會讓整個展示
講出不合理的故事。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from domain import (CATEGORY_LABEL_ZH, CATEGORY_SUPPLIER_TYPE,  # noqa: E402
                    SUPPLIER_TYPE_LABEL_ZH, MaterialCategory, SupplierType)

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_every_category_maps_to_a_supplier_type():
    assert set(CATEGORY_SUPPLIER_TYPE) == {c.value for c in MaterialCategory}
    assert set(CATEGORY_SUPPLIER_TYPE.values()) <= {t.value for t in SupplierType}


def test_every_category_and_type_has_a_chinese_label():
    """卡片與畫面顯示中文料別；少一個就會在畫面上露出英文代碼。"""
    assert set(CATEGORY_LABEL_ZH) == {c.value for c in MaterialCategory}
    assert set(SUPPLIER_TYPE_LABEL_ZH) == {t.value for t in SupplierType}


def test_every_category_has_a_gr_processing_default():
    days = CFG["receiving"]["gr_processing_days"]
    assert set(days) == {c.value for c in MaterialCategory}
    assert all(isinstance(v, int) and 0 <= v <= 30 for v in days.values())


def test_fabless_categories_are_gone():
    """回歸：情境已改為晶圓廠，載板與封測不該再出現。"""
    values = {c.value for c in MaterialCategory} | {t.value for t in SupplierType}
    assert not values & {"SUBSTRATE", "ASSEMBLY", "FOUNDRY", "OSAT", "WAFER"}
