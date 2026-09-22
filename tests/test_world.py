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


import random  # noqa: E402

import generate_data  # noqa: E402


def _world():
    random.seed(CFG["data_generation"]["seed"])
    from datetime import date
    as_of = date.fromisoformat(CFG["data_generation"]["as_of_date"])
    sups = generate_data.build_suppliers()
    mats = generate_data.build_materials(CFG["data_generation"]["n_materials"])
    pos = generate_data.build_pos(mats, sups, CFG["data_generation"]["n_purchase_orders"], as_of)
    return sups, mats, pos


def test_po_supplier_type_matches_material_category():
    """光阻單不能掛在氣體廠名下 —— 那是整個故事最容易被一眼看穿的破綻。"""
    sups, mats, pos = _world()
    stype = {s["supplier_id"]: s["supplier_type"] for s in sups}
    cat = {m["material_id"]: m["category"] for m in mats}
    for p in pos:
        assert stype[p["supplier_id"]] == CATEGORY_SUPPLIER_TYPE[cat[p["material_id"]]], p["po_no"]


def test_materials_carry_uom_and_gr_days_from_config():
    _, mats, _ = _world()
    days = CFG["receiving"]["gr_processing_days"]
    for m in mats:
        assert m["base_uom"] in {"PCS", "GAL", "CYL", "DRM"}
        assert m["gr_processing_days"] == days[m["category"]]


def test_all_categories_present():
    _, mats, _ = _world()
    assert {m["category"] for m in mats} == {c.value for c in MaterialCategory}


def test_fourteen_suppliers_with_unique_ids():
    sups, _, _ = _world()
    assert len(sups) == 14 == len({s["supplier_id"] for s in sups})


def test_handcrafted_pos_exist_with_the_email_sender_as_supplier():
    """
    手寫信件的寄件供應商，必須就是那張單在主檔上的供應商。
    對不上時，工具會把信對到別家的單，展示時一眼就穿幫。
    """
    from handcrafted_emails import HANDCRAFTED
    fixed = {p[0]: p for p in generate_data.FIXED_POS}
    for hc in HANDCRAFTED:
        for g in hc["ground_truth"]:
            assert g["po_no"] in fixed, (hc["email_id"], g["po_no"])
            assert fixed[g["po_no"]][2] == hc["supplier_id"], (hc["email_id"], g["po_no"])
