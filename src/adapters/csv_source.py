# -*- coding: utf-8 -*-
"""
CSV 資料來源（最初的實作，保留作為對照）。

它的價值不在於方便，而在於**對照**：
把它跟 SqliteSource 並排看，就能一眼看出整合 ERP 真正的難處在哪。

CSV 版本的 `reschedule_count`、`has_qualified_second_source`、
`share_of_period_demand` 都是「現成的一欄」——
但真實 ERP 裡它們都不存在，必須從變更文件、來源清單、請購單推導出來。

換句話說：**CSV 版本跳過了整合工作中最花時間的那一段。**
展示很方便，但不該假裝那就是接 ERP 的樣子。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import DataSource

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"


class CsvSource(DataSource):
    name = "csv"

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.dir = Path(data_dir or DATA)
        if not (self.dir / "po_master.csv").exists():
            raise FileNotFoundError(
                f"找不到 {self.dir / 'po_master.csv'}，"
                "請先執行： py src/generate_data.py")

    def _read(self, name: str) -> pd.DataFrame:
        return pd.read_csv(self.dir / name, encoding="utf-8-sig")

    def purchase_orders(self) -> pd.DataFrame:
        return self._read("po_master.csv")

    def materials(self) -> pd.DataFrame:
        df = self._read("materials.csv")
        # 舊版資料相容：Task 4（收貨處理天數）之前產生的 materials.csv
        # 沒有 base_uom／gr_processing_days 這兩欄。用 NULL／0 補齊，
        # 讓舊資料仍滿足資料合約；真實 data/ 要到 Task 10 才會用新版
        # generate_data.py 重新產生。
        if "base_uom" not in df.columns:
            df["base_uom"] = None
        if "gr_processing_days" not in df.columns:
            df["gr_processing_days"] = 0
        return df

    def suppliers(self) -> pd.DataFrame:
        return self._read("suppliers.csv")

    def describe(self) -> str:
        return "CSV 檔（欄位為預先算好的扁平資料，非 ERP 實際樣貌）"
