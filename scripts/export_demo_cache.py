# -*- coding: utf-8 -*-
"""
匯出部署用的種子快取（demo_cache/）。

===========================  為什麼需要這支腳本  ===========================
公開展示的網址有兩個問題：

1. **冷啟動太慢**：語意索引本機首次建立約需數分鐘（受每分鐘 token 上限影響），
   訪客不會等。
2. **每位訪客都在燒你的 API 配額**：首頁載入就要解析 25 封需要 LLM 的信。

解法：把「已經算好、而且內容可重現」的結果一併部署。
資料產生器使用固定亂數種子，prompt 內容每次都一樣，快取鍵在雲端也對得上。

只匯出**這次實際用到的**快取項目，不把開發過程中累積的舊 prompt 結果一起帶上去。
訪客問了範例以外的新問題時，才會真正呼叫 API（並受每次工作階段的額度上限保護）。

使用方式（需要 .env 裡有可用的金鑰）：
    py -X utf8 scripts/export_demo_cache.py
==========================================================================
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import llm.provider as prov  # noqa: E402
import pipeline  # noqa: E402
from rag.answer import EXAMPLE_QUESTIONS, answer  # noqa: E402
from rag.eval_questions import QUESTIONS  # noqa: E402
from rag.knowledge import build_cards  # noqa: E402
from rag.retriever import HybridRetriever  # noqa: E402

SEED_ROOT = ROOT / "demo_cache"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    provider = prov.get_provider()
    if not provider.available:
        raise SystemExit("找不到可用的 LLM 金鑰，無法匯出種子快取。請先設定 .env。")

    cfg = pipeline.load_config()
    prov.ACCESSED_KEYS.clear()

    print("① 執行主流程（解析信件）…")
    stats = pipeline.run(cfg=cfg, use_llm=True)["stats"]
    print(f"   升級至 LLM {stats['escalated']} 封，成功 {stats['llm_ok']} 封")

    print("② 建立語意索引…")
    retriever = HybridRetriever(build_cards(cfg), provider=provider)
    if not retriever.build_semantic_index():
        raise SystemExit(f"語意索引建立失敗：{retriever.semantic_error}")

    print("③ 預先回答範例問題…")
    ref = cfg["data_generation"]["as_of_date"]
    for q in EXAMPLE_QUESTIONS:
        res = answer(q, retriever, provider, reference_date=ref)
        flag = "OK" if res.get("llm") else "未取得 LLM 回答"
        print(f"   [{flag}] {q}")

    print("④ 寫出種子檔…")
    llm_dir = SEED_ROOT / "llm"
    if llm_dir.exists():
        shutil.rmtree(llm_dir)
    llm_dir.mkdir(parents=True)
    copied = 0
    for key in sorted(prov.ACCESSED_KEYS):
        src = prov.CACHE_DIR / f"{key}.json"
        if not src.exists():
            src = prov.SEED_CACHE_DIR / f"{key}.json"
        if src.exists() and src.resolve() != (llm_dir / src.name).resolve():
            shutil.copy2(src, llm_dir / src.name)
            copied += 1

    queries = list(dict.fromkeys(EXAMPLE_QUESTIONS + [q for _, q, _ in QUESTIONS]))
    info = retriever.export_seed(SEED_ROOT / "rag", queries)

    size = sum(p.stat().st_size for p in SEED_ROOT.rglob("*") if p.is_file())
    print(f"   LLM 回應快取 {copied} 筆、知識卡向量 {info['cards']} 張、"
          f"查詢向量 {info['queries']} 筆，合計 {size / 1024:.0f} KB")
    print(f"完成：{SEED_ROOT}")


if __name__ == "__main__":
    main()
