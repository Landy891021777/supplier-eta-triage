# -*- coding: utf-8 -*-
"""
資料來源 adapter。

    工具的判斷邏輯不該知道資料從哪裡來。

目前提供 csv 與 sqlite 兩種來源，以 config.yaml 的 `data_source` 切換。
真要接公司 ERP 時，只需要再實作一個 DataSource 子類別，
規則、評分、介面一行都不用改。
"""
from __future__ import annotations

from .base import (MATERIAL_CONTRACT, PO_CONTRACT, SUPPLIER_CONTRACT,
                   DataSource)
from .csv_source import CsvSource
from .sqlite_source import SqliteSource

_REGISTRY = {"csv": CsvSource, "sqlite": SqliteSource}


def get_source(name: str = "csv", **kwargs) -> DataSource:
    """
    依名稱建立資料來源。

    找不到指定來源時明確拋錯，不要安靜地退回預設值 ——
    使用者以為在讀 ERP、實際卻在讀 CSV，是最糟的失敗方式。
    """
    key = (name or "csv").strip().lower()
    if key not in _REGISTRY:
        raise ValueError(
            f"未知的資料來源 '{name}'。可用：{', '.join(_REGISTRY)}")
    return _REGISTRY[key](**kwargs)


__all__ = ["DataSource", "CsvSource", "SqliteSource", "get_source",
           "PO_CONTRACT", "MATERIAL_CONTRACT", "SUPPLIER_CONTRACT"]
