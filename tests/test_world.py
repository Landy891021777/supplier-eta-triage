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


# ---------------------------------------------------------------------------
# 分批交貨（Plan 3 Task 1）：build_pos 要能產生多筆交貨排程行
# ---------------------------------------------------------------------------
from datetime import date  # noqa: E402


def test_about_20_percent_of_generated_pos_split_into_two_schedule_lines():
    """
    領域假設：約 20% 的單分兩批交貨（先出一部分、其餘延後），
    第二批比第一批晚 7～30 天，兩批數量相加等於項次總量。

    用區間（10%～25%）而非精確比例斷言：機率抽樣本來就會有筆數浮動，
    抓死精確值只會讓測試跟著亂數實作細節碎掉，卻驗不出真正在意的行為。
    光罩一次只買 1 片，拆批會出現「先出 0.3 片」這種不合理的資料，因此
    光罩不該出現在拆批清單裡（MASK 的 qty 規格本來就只有 1，這裡再明確驗一次）。
    """
    _, mats, pos = _world()
    cat = {m["material_id"]: m["category"] for m in mats}
    fixed_nos = {f[0] for f in generate_data.FIXED_POS}
    generated = [p for p in pos if p["po_no"] not in fixed_nos]

    assert generated, "沒有非固定的生成單，測試前提不成立"
    for p in generated:
        assert "schedule" in p, p["po_no"]

    split = [p for p in generated if len(p["schedule"]) == 2]
    ratio = len(split) / len(generated)
    assert 0.10 <= ratio <= 0.25, f"分批比例 {ratio:.3f} 不在 10%~25% 區間"

    for p in split:
        assert cat[p["material_id"]] != MaterialCategory.MASK.value, p["po_no"]
        lines = p["schedule"]
        assert [ln[0] for ln in lines] == [1, 2], p["po_no"]
        assert sum(ln[2] for ln in lines) == p["qty"], p["po_no"]
        assert all(ln[2] >= 1 for ln in lines), p["po_no"]
        d1 = date.fromisoformat(lines[0][1])
        d2 = date.fromisoformat(lines[1][1])
        assert 7 <= (d2 - d1).days <= 30, (p["po_no"], d1, d2)
        assert d1 == date.fromisoformat(p["committed_date"]), p["po_no"]

    for p in generated:
        if len(p["schedule"]) == 1:
            assert p["schedule"][0] == (1, p["committed_date"], p["qty"]), p["po_no"]


def test_fixed_po_04188_is_split_matching_hc010():
    """
    手寫案例 HC-010 描述 PO-2026-04188（TG-Cu-90，共 20 片）分批交貨：
    8 片照原日期 2026-09-30、12 片延到 2026-11-15。這筆是固定資料，
    不能靠隨機決定是否分批，否則信件內容跟 ERP 資料會對不上。
    """
    _, _, pos = _world()
    po = next(p for p in pos if p["po_no"] == "PO-2026-04188")
    assert po["schedule"] == [(1, "2026-09-30", 8), (2, "2026-11-15", 12)]


def test_other_fixed_pos_are_not_split():
    """
    FIXED_POS 其餘的單依賴固定的單一承諾日（多處展示與規則測試以此為前提），
    不該被隨機拆批邏輯誤觸，只有明確標記 split 的那一筆才會分批。
    """
    _, _, pos = _world()
    fixed = {p[0]: p for p in generate_data.FIXED_POS}
    for po in pos:
        if po["po_no"] in fixed and po["po_no"] != "PO-2026-04188":
            assert len(po["schedule"]) == 1, po["po_no"]
