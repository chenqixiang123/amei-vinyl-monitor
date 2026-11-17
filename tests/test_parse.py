#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不联网的单元测试：验证页面解析和报警判断。

    python tests/test_parse.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import monitor as m  # noqa: E402

# 根据真实页面结构裁剪出来的样本
FIVE_OOS = """
<div class="sale-wrap"><div class='sale-ti'>(目前無現貨)</div></div>
<div class='sale-ti red'><b>出貨日期:2026 10月2日</b></div>
<div>張惠妹 偷故事的人 黑膠</div>
<div>定價 NT$ 1,840</div>
"""

FIVE_IN = """
<div>張惠妹 偷故事的人 黑膠</div>
<div class='sale-ti red'><b>出貨日期:2026 11月20日</b></div>
<div>定價 NT$ 1,840</div>
<button>加入購物車</button>
"""

CCR_OOS = """
<div class="article_name">偷故事的人 黑膠</div>
<div class="article_price">網路價 NT$ : <b>1,840</b>元</div>
<span class="cart_warn">無庫存</span>
"""

CCR_IN = """
<div class="article_name">偷故事的人 黑膠</div>
<div class="article_price">網路價 NT$ : <b>1,840</b>元</div>
<button>加入購物車</button>
"""


def snapshot(five: str, ccr: str, ship: str | None = None) -> dict:
    return {
        "five": {"status": five, "price": "NT$1,840", "ship_date": ship},
        "ccr": {"status": ccr, "price": "NT$1,840", "ship_date": None},
        "ship_date": ship,
    }


class TestParsing(unittest.TestCase):
    def test_five_out_of_stock(self):
        r = m.parse_five(FIVE_OOS)
        self.assertEqual(r["status"], "oos")
        self.assertEqual(r["ship_date"], "2026-10-02")
        self.assertEqual(r["price"], "NT$1,840")

    def test_five_in_stock_and_new_ship_date(self):
        r = m.parse_five(FIVE_IN)
        self.assertEqual(r["status"], "in")
        self.assertEqual(r["ship_date"], "2026-11-20")

    def test_five_page_changed(self):
        r = m.parse_five("<html>系統維護中</html>")
        self.assertEqual(r["status"], "unknown")

    def test_ccr_out_of_stock(self):
        self.assertEqual(m.parse_ccr(CCR_OOS)["status"], "oos")
        self.assertEqual(m.parse_ccr(CCR_OOS)["price"], "NT$1,840")

    def test_ccr_in_stock(self):
        self.assertEqual(m.parse_ccr(CCR_IN)["status"], "in")


class TestAlerts(unittest.TestCase):
    def test_restock_triggers_high(self):
        prev = snapshot("oos", "oos", "2026-10-02")
        cur = snapshot("in", "oos", "2026-10-02")
        alerts = m.decide_alerts(prev, cur)
        self.assertTrue(any(lvl == "high" and "五大唱片" in msg for lvl, msg in alerts))

    def test_ship_date_change_triggers_high(self):
        prev = snapshot("oos", "oos", "2026-10-02")
        cur = snapshot("oos", "oos", "2026-11-20")
        alerts = m.decide_alerts(prev, cur)
        self.assertTrue(any(lvl == "high" and "出貨日期" in msg for lvl, msg in alerts))

    def test_no_change_is_silent(self):
        prev = snapshot("oos", "oos", "2026-10-02")
        self.assertEqual(m.decide_alerts(prev, prev), [])

    def test_first_run_is_silent_when_out_of_stock(self):
        cur = snapshot("oos", "oos", "2026-10-02")
        self.assertEqual(m.decide_alerts({}, cur), [])

    def test_first_run_alerts_when_in_stock(self):
        cur = snapshot("in", "in", "2026-10-02")
        alerts = m.decide_alerts({}, cur)
        self.assertEqual(len([a for a in alerts if a[0] == "high"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
