#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
张惠妹《偷故事的人》黑胶 —— 补货监控（可在 GitHub Actions 上跑）

监控两个通路：
  1. 五大唱片 https://www.5music.com.tw/CDList-C.asp?cdno=439405678604
  2. 佳佳唱片 https://www.ccr.com.tw/goods/357085

判断逻辑：
  - 五大：出现「(目前無現貨)」＝无货；该字样消失且出现购物车按钮＝有货。
          另外单独跟踪「出貨日期」，日期被改成新日期＝第二批开放预购的最早信号。
  - 佳佳：出现「無庫存」＝无货；该字样消失且出现购物车按钮＝有货。

只在状态发生变化时发邮件，避免每天刷屏。
没配置 SMTP 环境变量时自动进入 dry-run，只打印不发信，方便本地测试。

用法：
    python monitor.py              # 正常跑一次
    python monitor.py --dry-run    # 只看结果，不发信，不写状态文件
    python monitor.py --test-email # 发一封测试邮件，验证 SMTP 配置
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

try:  # Windows 控制台默认不是 UTF-8，避免打印中文时崩掉
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 基础配置

# 台湾不使用夏令时，固定 UTC+8 即可，不依赖 tzdata（Windows 上没有）
TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "monitor_log.md"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PRODUCT = "偷故事的人"
LIST_PRICE = "NT$1,840"

FIVE_NAME = "五大唱片"
FIVE_URL = "https://www.5music.com.tw/CDList-C.asp?cdno=439405678604"

CCR_NAME = "佳佳唱片"
CCR_URL = "https://www.ccr.com.tw/goods/357085"

# 主监控确认有货时才扫描的备用通路
BACKUPS = [
    ("風谷購物網（环球官方）", "https://www.fangoods.com.tw/UMG/Default.aspx?ItemId=10291"),
    ("滾石購物網", "https://shop.rockmall.com.tw/product_view.php?id=100084"),
    ("老頭音樂", "https://www.lowrecordstw.com/products/amei-story-thief-lp.json"),
    ("小白兔唱片", "https://wwr.com.tw/albums/0602567103400"),
    ("博客來", "https://www.books.com.tw/products/0020204179"),
]

STATUS_TEXT = {
    "in": "有现货／可下单",
    "oos": "无货",
    "unknown": "无法判断（页面可能改版）",
    "error": "抓取失败",
}


# ---------------------------------------------------------------- 抓取工具


def decode_body(raw: bytes) -> str:
    for enc in ("utf-8", "big5", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def http_get(url: str, timeout: int = 30, tries: int = 3) -> str:
    """带重试的 GET。Accept-Encoding: identity，省得处理 gzip。"""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/json,*/*;q=0.8",
                    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if not raw:
                raise RuntimeError("返回内容为空")
            return decode_body(raw)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"抓取失败：{last}")


def text_of(html: str) -> str:
    """把 HTML 压成一段纯文本，用于兜底匹配。"""
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t)


def grab_price(html: str) -> str | None:
    m = re.search(r"(?:定價|售價|網路價|建議售價)[^0-9]{0,12}([0-9][0-9,]*)", html)
    return f"NT${m.group(1)}" if m else None


# ---------------------------------------------------------------- 各通路判断


def parse_five(html: str) -> dict:
    """纯解析，不联网，便于单元测试。"""
    if PRODUCT not in html:
        return {"status": "unknown", "note": "页面未出现商品名，可能改版或拦截", "price": None, "ship_date": None}

    out_of_stock = "目前無現貨" in html or "目前無现货" in html
    cart = any(k in html for k in ("放入購物車", "加入購物車", "直接購買"))

    if out_of_stock:
        status = "oos"
    elif cart:
        status = "in"
    else:
        status = "unknown"

    ship = None
    m = re.search(
        r"出貨日期\s*[:：]?\s*(20\d{2})\s*[年.\-/]?\s*(\d{1,2})\s*[月.\-/]?\s*(\d{1,2})\s*日?",
        html,
    )
    if m:
        ship = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    return {"status": status, "note": "", "price": grab_price(html) or LIST_PRICE, "ship_date": ship}


def check_five() -> dict:
    return parse_five(http_get(FIVE_URL))


def parse_ccr(html: str) -> dict:
    """纯解析，不联网，便于单元测试。"""
    if PRODUCT not in html:
        return {"status": "unknown", "note": "页面未出现商品名，可能改版或拦截", "price": None, "ship_date": None}

    out_of_stock = "無庫存" in html or "无库存" in html
    cart = any(k in html for k in ("放入購物車", "加入購物車", "直接購買"))

    if out_of_stock:
        status = "oos"
    elif cart:
        status = "in"
    else:
        status = "unknown"

    return {"status": status, "note": "", "price": grab_price(html), "ship_date": None}


def check_ccr() -> dict:
    return parse_ccr(http_get(CCR_URL))


def check_backups() -> list[dict]:
    """只在主监控出现有货时调用，结果作为邮件的补充信息。"""
    results = []
    for name, url in BACKUPS:
        item = {"name": name, "url": url, "state": "抓取失败", "ok": False}
        try:
            if url.endswith(".json"):  # 老頭音樂：Shopify 商品接口
                data = json.loads(http_get(url))
                variants = data.get("variants") or []
                sellable = any(v.get("available") for v in variants)
                item["state"] = "可下单" if sellable else "预购中／不可下单"
                item["ok"] = True
            else:
                html = http_get(url, timeout=25, tries=2)
                if PRODUCT not in html and "6710340" not in html:
                    item["state"] = "已下架或页面已变更"
                elif any(k in html for k in ("售完", "暫無庫存", "無庫存", "目前無現貨", "缺貨")):
                    item["state"] = "无货"
                elif any(k in html for k in ("放入購物車", "加入購物車", "直接購買")):
                    item["state"] = "可下单"
                else:
                    item["state"] = "无法判断"
                item["ok"] = True
        except Exception as exc:  # noqa: BLE001
            item["state"] = f"抓取失败（{type(exc).__name__}）"
        results.append(item)
        time.sleep(0.5)
    return results


# ---------------------------------------------------------------- 状态与提醒


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def decide_alerts(prev: dict, cur: dict) -> list[tuple[str, str]]:
    """返回 [(级别, 说明)]。级别：high / warn / low。"""
    alerts: list[tuple[str, str]] = []

    for key, name in (("five", FIVE_NAME), ("ccr", CCR_NAME)):
        old = (prev.get(key) or {}).get("status")
        new = cur[key]["status"]
        if new == "in" and old != "in":
            tag = "首次发现现货" if not old else f"{STATUS_TEXT.get(old, old)} → 有现货"
            alerts.append(("high", f"{name} 出现现货／可下单（{tag}）"))
        elif new == "unknown" and old != "unknown":
            alerts.append(("warn", f"{name} 页面状态无法判断，可能改版，需要人工看一眼"))
        elif new == "error" and old != "error":
            alerts.append(("warn", f"{name} 本次抓取失败"))
        elif new == "oos" and old == "in":
            alerts.append(("low", f"{name} 已恢复无货（上一轮还在售）"))

    old_ship = prev.get("ship_date")
    new_ship = cur.get("ship_date")
    if old_ship and new_ship and new_ship != old_ship:
        alerts.append(("high", f"{FIVE_NAME}「出貨日期」由 {old_ship} 改为 {new_ship}，很可能是第二批开放预购"))
    if not old_ship and new_ship and prev:
        alerts.append(("low", f"首次记录到{FIVE_NAME}出货日期：{new_ship}"))

    return alerts


def append_log(now: datetime, cur: dict, alerts: list[tuple[str, str]]) -> None:
    row = (
        f"| {now.strftime('%Y-%m-%d %H:%M')} "
        f"| {STATUS_TEXT.get(cur['five']['status'], cur['five']['status'])} "
        f"| {STATUS_TEXT.get(cur['ccr']['status'], cur['ccr']['status'])} "
        f"| {('；'.join(a[1] for a in alerts)) if alerts else '无变化'} |\n"
    )
    header = (
        "# 补货监控日志（云端 GitHub Actions 自动写入）\n\n"
        "| 时间（GMT+8） | 五大唱片 | 佳佳唱片 | 备注 |\n"
        "|---|---|---|---|\n"
    )
    if not LOG_PATH.exists():
        LOG_PATH.write_text(header + row, encoding="utf-8")
        return

    content = LOG_PATH.read_text(encoding="utf-8")
    marker = "|---|---|---|---|\n"
    idx = content.find(marker)
    if idx == -1:  # 文件被改坏了，重建
        LOG_PATH.write_text(header + row, encoding="utf-8")
        return
    insert_at = idx + len(marker)
    LOG_PATH.write_text(content[:insert_at] + row + content[insert_at:], encoding="utf-8")


# ---------------------------------------------------------------- 邮件


def smtp_configured() -> bool:
    return all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))


def send_mail(subject: str, text: str, html: str | None = None, to_override: str | None = None) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT") or 465)
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    recipients = [x.strip() for x in (to_override or os.getenv("MAIL_TO") or user).split(",") if x.strip()]

    msg = EmailMessage()
    msg["From"] = formataddr(("黑胶补货监控", user))
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)


def build_mail(now: datetime, cur: dict, alerts: list[tuple[str, str]], backups: list[dict]) -> tuple[str, str, str]:
    high = [a for a in alerts if a[0] == "high"]
    if high:
        subject = f"[有货] {PRODUCT} 黑胶：{high[0][1][:40]}"
    elif alerts:
        subject = f"[提醒] {PRODUCT} 黑胶监控：{alerts[0][1][:40]}"
    else:
        subject = f"[状态] {PRODUCT} 黑胶：两家仍无货（{now.strftime('%m-%d %H:%M')}）"

    lines = [f"{PRODUCT} 黑胶补货监控 · {now.strftime('%Y-%m-%d %H:%M')}（台湾时间）", ""]
    if alerts:
        lines.append("变化：")
        lines += [f"  · {a[1]}" for a in alerts]
        lines.append("")
    else:
        lines.append("两家通路均无变化，仍在缺货状态。")
        lines.append("")

    lines.append("当前状态：")
    for key, name, url in (("five", FIVE_NAME, FIVE_URL), ("ccr", CCR_NAME, CCR_URL)):
        d = cur[key]
        extra = f"，出货日期 {d['ship_date']}" if d.get("ship_date") else ""
        price = f"，{d['price']}" if d.get("price") else ""
        lines.append(f"  · {name}：{STATUS_TEXT.get(d['status'], d['status'])}{price}{extra}")
        lines.append(f"    {url}")

    if backups:
        lines += ["", "备用通路（顺带扫的）："]
        for b in backups:
            lines.append(f"  · {b['name']}：{b['state']}")
            lines.append(f"    {b['url']}")

    lines += [
        "",
        "提示：这张 2026 再版补货通常几小时内售罄，收到有货通知建议立刻下单。",
        "五大「此商品無取貨付款」，只能刷卡或 ATM，建议提前准备好付款方式。",
    ]
    text = "\n".join(lines)

    color = {"high": "#c0392b", "warn": "#b9770e", "low": "#5d6d7e"}
    rows = ""
    for key, name, url in (("five", FIVE_NAME, FIVE_URL), ("ccr", CCR_NAME, CCR_URL)):
        d = cur[key]
        state = STATUS_TEXT.get(d["status"], d["status"])
        badge_bg = "#c0392b" if d["status"] == "in" else ("#7f8c8d" if d["status"] in ("oos",) else "#b9770e")
        rows += (
            f'<tr><td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">'
            f'<a href="{url}" style="color:#1a5490;text-decoration:none;">{name}</a></td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;">'
            f'<span style="background:{badge_bg};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px;">{state}</span></td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#4b5563;">'
            f'{d.get("price") or "—"}'
            + (f'<br><span style="font-size:12px;color:#6b7280;">出货日期 {d["ship_date"]}</span>' if d.get("ship_date") else "")
            + "</td></tr>"
        )

    alert_html = ""
    if alerts:
        items = "".join(
            f'<li style="margin:6px 0;color:{color.get(level, "#333")};">{msg}</li>' for level, msg in alerts
        )
        alert_html = (
            '<div style="background:#fff8f0;border-left:4px solid #c0392b;padding:12px 16px;margin:0 0 18px 0;border-radius:4px;">'
            f'<div style="font-weight:600;margin-bottom:6px;">本次变化</div>'
            f'<ul style="margin:0;padding-left:20px;">{items}</ul></div>'
        )
    else:
        alert_html = (
            '<div style="background:#f4f6f8;border-left:4px solid #95a5a6;padding:12px 16px;margin:0 0 18px 0;border-radius:4px;">'
            "两家通路均无变化，仍在缺货状态。</div>"
        )

    backup_html = ""
    if backups:
        lis = "".join(
            f'<li style="margin:4px 0;"><a href="{b["url"]}" style="color:#1a5490;">{b["name"]}</a>：{b["state"]}</li>'
            for b in backups
        )
        backup_html = (
            '<div style="margin-top:20px;"><div style="font-weight:600;margin-bottom:6px;">备用通路</div>'
            f'<ul style="margin:0;padding-left:20px;color:#4b5563;font-size:14px;">{lis}</ul></div>'
        )

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f9fafb;font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:620px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);">
  <div style="padding:20px 24px;border-bottom:1px solid #eef0f2;">
    <div style="font-size:18px;font-weight:600;color:#111827;">{PRODUCT} 黑胶 · 补货监控</div>
    <div style="font-size:13px;color:#6b7280;margin-top:4px;">{now.strftime('%Y-%m-%d %H:%M')}（台湾时间）</div>
  </div>
  <div style="padding:20px 24px;">
    {alert_html}
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr style="background:#f9fafb;color:#6b7280;text-align:left;font-size:12px;">
        <th style="padding:8px 12px;">通路</th><th style="padding:8px 12px;">状态</th><th style="padding:8px 12px;">参考价</th>
      </tr>
      {rows}
    </table>
    {backup_html}
    <div style="margin-top:22px;padding:12px 16px;background:#f4f6f8;border-radius:4px;font-size:13px;color:#4b5563;line-height:1.7;">
      这张 2026 再版补货通常几小时内售罄，有货就立刻下单。<br>
      五大「此商品無取貨付款」，只能刷卡或 ATM，提前准备好付款方式。
    </div>
  </div>
</div>
</body></html>"""
    return subject, text, html


# ---------------------------------------------------------------- 主流程


def main() -> int:
    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args
    can_mail = smtp_configured()
    test_email = "--test-email" in args
    force_email = os.getenv("FORCE_EMAIL", "").lower() in ("1", "true", "yes")

    now = datetime.now(TZ)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开始检查")

    prev = load_state()
    cur: dict = {"checked_at": now.isoformat(timespec="seconds"), "ship_date": None}

    for key, fn, name in (("five", check_five, FIVE_NAME), ("ccr", check_ccr, CCR_NAME)):
        try:
            result = fn()
            cur[key] = result
            print(f"  {name}: {result['status']}  价格={result.get('price')}  出货={result.get('ship_date')}")
        except Exception as exc:  # noqa: BLE001
            cur[key] = {"status": "error", "note": str(exc)[:200], "price": None, "ship_date": None}
            print(f"  {name}: 抓取失败 -> {exc}")

    cur["ship_date"] = (cur.get("five") or {}).get("ship_date")

    if test_email:
        if not smtp_configured():
            print("未配置 SMTP，无法测试邮件。请先设置 SMTP_HOST / SMTP_USER / SMTP_PASS。")
            return 2
        subject, text, html = build_mail(now, cur, [("low", "这是一封测试邮件，用于验证 SMTP 配置是否可用。")], [])
        send_mail("[测试] " + subject, text, html)
        print("测试邮件已发送。")
        return 0

    alerts = decide_alerts(prev, cur)
    print(f"  变化：{alerts if alerts else '无'}")

    need_mail = bool(alerts) or force_email
    any_in = any(cur[k]["status"] == "in" for k in ("five", "ccr"))

    backups = []
    if any_in:
        print("  检测到有货，顺带扫描备用通路…")
        backups = check_backups()
        for b in backups:
            print(f"    {b['name']}: {b['state']}")

    # 连续抓取失败两次才发告警，避免偶发网络问题刷屏
    both_error = all(cur[k]["status"] == "error" for k in ("five", "ccr"))
    error_streak = int(prev.get("error_streak") or 0)
    error_streak = error_streak + 1 if both_error else 0
    cur["error_streak"] = error_streak
    if both_error and error_streak == 2:
        alerts.append(("warn", "连续两轮两家都抓取失败，请检查网络或页面是否改版"))
        need_mail = True

    if need_mail:
        subject, text, html = build_mail(now, cur, alerts, backups)
        if dry_run:
            print("\n--- DRY RUN，不发送邮件 ---")
            print("主题：" + subject)
            print(text)
        elif not can_mail:
            print("\n未配置 SMTP（SMTP_HOST / SMTP_USER / SMTP_PASS），跳过发送。邮件内容：")
            print(text)
        else:
            try:
                send_mail(subject, text, html)
                print(f"  已发送邮件：{subject}")
            except Exception as exc:  # noqa: BLE001
                print(f"  邮件发送失败：{exc}")
                print(text)
    else:
        print("  无变化，不发送邮件")

    if dry_run:
        print("（dry-run：不写入 state.json / monitor_log.md）")
        return 0

    append_log(now, cur, alerts)
    save_state(
        {
            "last_run": now.isoformat(timespec="seconds"),
            "five": cur["five"],
            "ccr": cur["ccr"],
            "ship_date": cur["ship_date"],
            "error_streak": error_streak,
        }
    )
    print("  状态已保存")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"未捕获异常：{exc}", file=sys.stderr)
        raise SystemExit(1)
