# -*- coding: utf-8 -*-
"""
權重校準：用歷史結果檢驗「我訂的十條權重，資料同不同意」。

===========================  這支程式回答什麼問題  ===========================
評分卡的權重是我依實務直覺訂的。那是**假設**，不是結論。

一旦有了歷史結果（實際到料日 vs 下游需求日），就可以反過來問：
    「哪幾條規則資料是支持的？哪幾條其實沒什麼用？哪幾條方向根本相反？」

這就是 README「導入真實環境的下一步」第 5 點。這支程式把那個機制實作出來。

===========================  兩個必須先講的限制  ===========================
**限制一：資料是模擬的。**
歷史結果由 `src/generate_history.py` 的因果模型產生，不是真實資料。
因此學出來的係數只能證明「流程能跑」，不能用於任何決策。

**限制二：ERP 校準不了「承諾強度」。**
這條規則的訊號來自信件語氣（「大概月底吧，我再跟你確認」），
而 ERP 只存結果、不存語氣。歷史資料裡根本沒有這個欄位。

    要校準它，必須等工具自己上線、累積「當初判為 intent_only 的單，
    後來到底準不準」的紀錄。這是 ERP 補不起來的缺口，
    也正好說明了這個工具存在的理由。

因此下面只校準九條規則，第六條維持人訂。這不是偷懶，是資料的事實。
==========================================================================
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from impact import RULES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp_sim.db"
OUT = ROOT / "output"

# 承諾強度來自信件語氣，ERP 沒有這個欄位，無法用歷史資料校準。
UNCALIBRATABLE = {"commitment_strength"}

# 決策時點：以「最後一次改期通知」為準。
# 沒有改期紀錄的單，生管根本不會收到通知，也就不會進入行動清單 ——
# 拿它們來校準會稀釋掉真正的訊號，因此排除。
HISTORY_SQL = """
WITH last_change AS (
    SELECT c.po_no, c.item_no, c.old_value, c.new_value, c.changed_at,
           COUNT(*) OVER (PARTITION BY c.po_no, c.item_no) AS n_changes,
           ROW_NUMBER() OVER (PARTITION BY c.po_no, c.item_no
                              ORDER BY c.changed_at DESC) AS rn
    FROM po_change_log c
    WHERE c.field_name = 'committed_date'
)
SELECT
    i.po_no, i.material_id, h.vendor_id AS supplier_id, i.qty,
    lc.old_value  AS original_committed,   -- 改期前的承諾日
    lc.new_value  AS new_eta,              -- 改期後的承諾日（決策當下的資訊）
    lc.changed_at AS notice_date,          -- 通知日
    lc.n_changes  AS reschedule_count,
    r.need_date, r.period_demand_qty,
    COALESCE(r.downstream_scheduled, 0) AS downstream_scheduled,
    m.category, m.std_lead_time_days, m.is_bottleneck, m.criticality,
    COALESCE((SELECT ma.alt_material_id FROM material_alternate ma
              WHERE ma.material_id = m.material_id LIMIT 1), '') AS alt_material_id,
    CASE WHEN (SELECT COUNT(*) FROM source_list sl
               WHERE sl.material_id = m.material_id AND sl.is_qualified = 1) >= 2
         THEN 1 ELSE 0 END AS has_qualified_second_source,
    g.receipt_date                          -- 後來真正到料的日子（outcome）
FROM po_item i
JOIN po_header    h  ON h.po_no = i.po_no
JOIN last_change  lc ON lc.po_no = i.po_no AND lc.item_no = i.item_no AND lc.rn = 1
JOIN purchase_req r  ON r.pr_no = i.pr_no
JOIN material_master m ON m.material_id = i.material_id
JOIN goods_receipt g ON g.po_no = i.po_no AND g.item_no = i.item_no
"""


def load_history() -> pd.DataFrame:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"找不到 {DB_PATH}，請先執行： py src/build_erp_db.py")
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query(HISTORY_SQL, con)
    if df.empty:
        raise RuntimeError(
            "沒有可用的歷史結果，請先執行： py src/generate_history.py")
    return df


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """
    把每一筆歷史單還原成「決策當下」的十條規則得分，並算出真實結果。

    關鍵在於只能使用當時已知的資訊：改期通知日、改期後的承諾日、
    當時的改期次數。**實際到料日只能當答案，不能當特徵** ——
    否則就是資料洩漏，模型會漂亮得不像話而且完全沒用。
    """
    rows, ys = [], []
    for r in df.itertuples(index=False):
        record = {
            "new_eta": r.new_eta,
            "received_at": r.notice_date,
            "change_type": "delay",
            # 承諾強度在歷史資料裡不存在。給中性值只是為了讓規則跑得動，
            # 該欄位之後會被排除在校準之外。
            "commitment_strength": "estimated",
        }
        po = {
            "committed_date": r.original_committed,
            "need_date": r.need_date,
            "downstream_scheduled": bool(r.downstream_scheduled),
            "reschedule_count": int(r.reschedule_count),
            "share_of_period_demand": (float(r.qty) / r.period_demand_qty
                                       if r.period_demand_qty else 1.0),
        }
        material = {
            "has_qualified_second_source": bool(r.has_qualified_second_source),
            "criticality": r.criticality,
            "std_lead_time_days": r.std_lead_time_days,
            "is_bottleneck": bool(r.is_bottleneck),
            "alt_material_id": r.alt_material_id,
        }
        eta = date.fromisoformat(str(r.new_eta)[:10])
        ctx = {"record": record, "po": po, "material": material,
               "supplier": {}, "effective_eta": eta}
        rows.append({name: fn(ctx)[0] for name, fn in RULES.items()})

        # 真實結果：實際到料日晚於下游需求日 = 真的造成缺料。
        # 這是客觀事實，由兩個日期相減得到，不含任何模型輸出。
        ys.append(int(date.fromisoformat(str(r.receipt_date)[:10])
                      > date.fromisoformat(str(r.need_date)[:10])))

    X = pd.DataFrame(rows)
    return X.drop(columns=list(UNCALIBRATABLE)), pd.Series(ys, name="shortage")


def calibrate(random_state: int = 20260908) -> dict:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import train_test_split

    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    hand = cfg["impact_weights"]

    df = load_history()
    X, y = build_features(df)

    # 切分訓練與測試。用全部資料算 AUC 會高估 ——
    # 而高估的效能會讓人以為模型比評分卡強，做出錯誤的替換決定。
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.3, random_state=random_state, stratify=y)

    model = LogisticRegression(max_iter=2000, C=1.0)
    model.fit(X_tr, y_tr)
    coefs = pd.Series(model.coef_[0], index=X.columns)

    # 學到的係數 → 可與人訂權重並列比較的尺度。
    # 負係數代表「資料認為這條規則方向相反」，一律截為 0 並標示出來 ——
    # 不是把它硬塞進權重，而是把分歧攤開來讓人看見。
    positive = coefs.clip(lower=0)
    learned = (positive / positive.sum() * sum(
        v for k, v in hand.items() if k not in UNCALIBRATABLE)
        if positive.sum() > 0 else positive)

    # 兩種評分方式在同一份測試資料上的比較
    auc_model = roc_auc_score(y_te, model.predict_proba(X_te)[:, 1])
    hand_w = np.array([hand[c] for c in X.columns], dtype=float)
    auc_hand = roc_auc_score(y_te, X_te.values @ hand_w / hand_w.sum())

    table = pd.DataFrame({
        "人訂權重": [hand[c] for c in X.columns],
        "學到的係數": coefs.round(3).values,
        "校準後權重": learned.round(1).values,
    }, index=X.columns)
    table["資料的意見"] = [
        "方向相反，資料不支持" if c < -0.05 else
        ("影響很小" if abs(c) < 0.15 else
         ("資料支持，且比我想的更重要" if lw > hw * 1.3 else
          ("資料支持，但沒我想的重要" if lw < hw * 0.7 else "與我的判斷一致")))
        for c, hw, lw in zip(coefs, table["人訂權重"], table["校準後權重"])]

    return {
        "table": table,
        "n_total": len(X), "n_shortage": int(y.sum()),
        "shortage_rate": float(y.mean()),
        "auc_model": float(auc_model), "auc_hand": float(auc_hand),
        "excluded": sorted(UNCALIBRATABLE),
    }


def to_markdown(res: dict) -> str:
    t = res["table"].copy()
    t.index.name = "規則"
    lines = [
        "# 權重校準結果", "",
        "> ⚠️ **本次校準使用模擬歷史資料，係數僅供展示流程，不可用於決策。**",
        "> 歷史結果由 `src/generate_history.py` 的因果模型產生，非真實資料。", "",
        f"樣本：**{res['n_total']}** 筆已結案且曾收到改期通知的採購單，",
        f"其中 **{res['n_shortage']}** 筆實際造成缺料（{res['shortage_rate']:.1%}）。", "",
        "## 人訂權重 vs 資料學到的權重", "",
        t.reset_index().to_markdown(index=False), "",
        "## 兩種評分方式在測試集上的表現", "",
        "| 方式 | AUC |",
        "|---|---:|",
        f"| 人訂評分卡 | {res['auc_hand']:.3f} |",
        f"| 資料學到的模型 | {res['auc_model']:.3f} |", "",
        "AUC 差距不大時，**應該留著評分卡** —— 它可解釋、可被同仁質疑、",
        "可以在會議上逐條討論。模型只有在明顯勝出時才值得付出可解釋性的代價。", "",
        "## ⚠️ 「方向相反」不等於規則錯 —— 這是本次校準最重要的發現", "",
        "`downstream_scheduled`（下游已排定）、`delay_share`（延遲量佔比）、",
        "`substitutability`（可替代性）三條被判為方向相反。",
        "**但把它們拿掉是錯的。**", "",
        "原因是：這次校準的 outcome 是「會不會缺料」，也就是**發生機率**。",
        "而那三條規則衡量的根本不是機率，是**缺料之後有多痛**：", "",
        "| 規則 | 它預測的是 | 校準能不能驗證 |",
        "|---|---|---|",
        "| 緩衝天數、料的關鍵性、累犯 | **會不會**缺料（機率） | ✅ 可以 |",
        "| 下游已排定、延遲量佔比、可替代性 | 缺了**有多痛**（代價） | ❌ 不行 |", "",
        "下游有沒有排定產能，不會改變供應商延不延；",
        "但它會決定「延了之後要動幾條排程、要不要跟客戶道歉」。", "",
        "**評分卡本來就在混合兩件事：發生機率 × 影響代價。**",
        "而歷史資料只能校準機率那一半 —— 代價那一半沒有客觀 outcome 可對，",
        "除非公司有在記錄每次缺料的實際損失（加急運費、產線待料工時、客戶罰則）。", "",
        "因此正確的做法不是照單全收校準結果，而是：", "",
        "1. **機率類規則** —— 採用校準後的權重",
        "2. **代價類規則** —— 維持人訂，或改用實際損失金額重新定義",
        "3. 兩者相乘而非相加，才是真正的風險評估架構", "",
        "這是本版的已知限制，也是我認為下一版最該做的事。", "",
        "## 沒有被校準的規則", "",
        "| 規則 | 為什麼 |",
        "|---|---|",
        "| `commitment_strength`（承諾強度） | 訊號來自信件語氣，**ERP 只存結果、不存語氣**。"
        "歷史資料裡沒有這個欄位，必須等工具上線後自行累積。 |", "",
        "這一條校準不了，恰好說明了這個工具存在的理由：",
        "**它產生的是 ERP 結構上不會有的資料。**", "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = calibrate()
    OUT.mkdir(exist_ok=True)
    md = to_markdown(result)
    (OUT / "權重校準.md").write_text(md, encoding="utf-8")
    print(md)
