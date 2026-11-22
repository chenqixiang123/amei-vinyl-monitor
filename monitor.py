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

只在状态发生变化时发邮件，避免刷屏。
没配置 SMTP 环境变量时自动进入 dry-run，只打印不发信，方便本地测试。

高频轮询（每分钟一次）下的写仓库策略：
  - 状态有变化  → 立刻写日志 + 写状态，并让 workflow 提交（这是去重的锁，
                  不提交的话下一分钟会拿旧状态再比一次，重复发信）
  - 状态无变化  → 一天只在第一次运行时写一行「每日快照」，
                  其余 1400 多次运行完全不碰仓库，不产生 commit

用法：
    python monitor.py                 # 正常跑一次
    python monitor.py --dry-run        # 只看结果，不发信，不写状态文件
    python monitor.py --test-email     # 发一封测试邮件，验证 SMTP 配置
    python monitor.py --force-write    # 忽略「今日已写过」，强制写日志+状态
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

# 高频轮询时，同一类 warn / low 提醒多少小时内只发一次（high 级不受限）
WARN_COOLDOWN_HOURS = 6

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

ROCK_NAME = "滾石購物網"
ROCK_URL = "https://shop.rockmall.com.tw/product_view.php?id=100084"

# 只在「本商品的購物車區塊」里认这几个字样，避免被页面推荐位里别的商品的按钮骗到
CART_LABELS = ("加入購物車", "放入購物車", "加入購物车", "立即購買", "直接購買")

# 主监控确认有货时才扫描的备用通路（滾石已升级为正式监控通路，见 CHANNELS）
BACKUPS = [
    ("風谷購物網（环球官方）", "https://www.fangoods.com.tw/UMG/Default.aspx?ItemId=10291"),
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


# ---------------------------------------------------------------- 滾石購物網
# 页面是 365shop 的商城模板，坑比较多：
#   - 售完时不显示「加入購物車」，只有一个 disabled 的「售完」按钮
#   - 但整页别处（推荐位、隐藏模板）也可能出现「購物車」字样，按整页匹配会误判
# 所以只看 id="product_mobile_show_box" 到 </form> 之间的「本商品购物车区块」。


def _rock_cart_zone(html: str) -> str:
    """切出本商品的购买区，找不到就返回空串。"""
    m = re.search(r'id="product_mobile_show_box"', html)
    if not m:
        return ""
    end = html.find("</form>", m.start())
    return html[m.start() : end if end != -1 else m.start() + 8000]


def _has_enabled_cart(html: str) -> bool:
    """区块里是否存在「没被 disabled」的购物车按钮。"""
    for mo in re.finditer(r"<(button|a|input)\b[^>]*>", html, flags=re.I):
        tag = mo.group(0)
        low = tag.lower()
        if "disabled" in low or "not-allowed" in low:
            continue
        hay = tag + html[mo.end() : mo.end() + 60]
        if any(k in hay for k in CART_LABELS):
            return True
    return False


def parse_rock(html: str) -> dict:
    """纯解析，不联网，便于单元测试。"""
    plain = text_of(html)
    if PRODUCT not in plain and "6710340" not in plain:
        return {"status": "unknown", "note": "页面未出现商品名，可能改版或拦截", "price": None, "ship_date": None}

    zone = _rock_cart_zone(html)
    if not zone:
        return {"status": "unknown", "note": "找不到购物车区块，页面可能改版", "price": grab_price(plain), "ship_date": None}

    # 「售完」是页面上真实可见的按钮文字，优先采信
    sold = "售完" in zone or "已售完" in zone

    if sold:
        status, note = "oos", ""
    elif _has_enabled_cart(zone):
        status, note = "in", ""
    else:
        status, note = "unknown", "购物车区块里既没「售完」也没可用按钮"

    ship = None
    m = re.search(r"【預定發行】\s*(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", plain)
    if m:
        ship = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    return {"status": status, "note": note, "price": grab_price(plain) or LIST_PRICE, "ship_date": ship}


def check_rock() -> dict:
    return parse_rock(http_get(ROCK_URL))


# 正式监控的通路：顺序决定邮件表格和日志列的顺序
def channels() -> tuple:
    """写成函数而不是常量——单元测试要能替换 check_five / check_ccr / check_rock。"""
    return (
        ("five", FIVE_NAME, check_five),
        ("ccr", CCR_NAME, check_ccr),
        ("rock", ROCK_NAME, check_rock),
    )


CHANNEL_URLS = {"five": FIVE_URL, "ccr": CCR_URL, "rock": ROCK_URL}

# 日志表里状态列的顺序与表头
LOG_STATUS_KEYS = ("five", "ccr", "rock")


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


def confirm_in(prev: dict, cur: dict, wait: int = 15) -> None:
    """每分钟跑一次时，页面抖动很容易造成「有货」误报。

    从无货变成有货的这一刻，隔 15 秒再看一眼：两次都说有货才算数，
    否则以复查结果为准（一般会打回 oos，等于这次不报警、不提交）。
    只在「刚变成有货」时触发，平时不多花时间。
    """
    for key, name, fn in channels():
        if cur[key]["status"] != "in":
            continue
        if (prev.get(key) or {}).get("status") == "in":
            continue  # 上一轮就已经有货，不用复查
        print(f"  {name} 首次发现现货，{wait} 秒后复查确认…")
        time.sleep(wait)
        try:
            again = fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} 复查失败（{exc}），按首次结果处理，请人工确认")
            continue
        label = STATUS_TEXT.get(again["status"], again["status"])
        if again["status"] == "in":
            print(f"  {name} 复查通过，确认有货")
        else:
            print(f"  {name} 复查为「{label}」，判为页面抖动，本次不计")
        cur[key] = again


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def gh_output(pairs: dict) -> None:
    """写 GitHub Actions 的 step output，让 workflow 决定要不要提交。

    不在 Actions 里跑时（本地）静默跳过。
    """
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in pairs.items():
                if isinstance(v, bool):
                    v = "true" if v else "false"
                f.write(f"{k}={v}\n")
    except Exception as exc:  # noqa: BLE001
        print(f"  写 GITHUB_OUTPUT 失败：{exc}")


def apply_cooldown(
    alerts: list[tuple[str, str]],
    prev: dict,
    now: datetime,
    hours: int = WARN_COOLDOWN_HOURS,
) -> tuple[list[tuple[str, str]], dict]:
    """高频轮询专用：warn / low 同类提醒 N 小时内只留一条，high 全部放行。

    每分钟跑一次时，页面抖动（偶发 500、CDN 回源、临时改版）会让同一类警告
    反复触发。high（真的上货了）必须第一时间发，不能压。
    返回 (过滤后的 alerts, 要写回 state 的「已提醒时间表」)。
    """
    kept: dict[str, str] = {}
    cutoff = now - timedelta(days=7)  # 7 天前的记录直接清掉，防止文件一直变大
    for key, stamp in (prev.get("alerted") or {}).items():
        try:
            if datetime.fromisoformat(stamp) > cutoff:
                kept[key] = stamp
        except Exception:  # noqa: BLE001
            continue

    result: list[tuple[str, str]] = []
    for level, msg in alerts:
        if level == "high":
            result.append((level, msg))
            continue
        key = f"{level}:{msg[:30]}"
        last = kept.get(key)
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=hours):
                    print(f"  同类提醒 {hours} 小时内已发过，跳过：{msg[:34]}")
                    continue
            except Exception:  # noqa: BLE001
                pass
        kept[key] = now.isoformat(timespec="seconds")
        result.append((level, msg))
    return result, kept


def decide_alerts(prev: dict, cur: dict) -> list[tuple[str, str]]:
    """返回 [(级别, 说明)]。级别：high / warn / low。"""
    alerts: list[tuple[str, str]] = []

    for key, name, _fn in channels():
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


LOG_INTRO = (
    "# 补货监控日志（云端 GitHub Actions 自动写入）\n\n"
    "> 每分钟检查一次，但只在**状态变化**或**每天第一次运行**时写一行，\n"
    "> 所以这张表约等于「每日快照 + 变化流水」，不会一天几百行。\n\n"
)
LOG_HEAD_ROW = "| 时间（GMT+8） | 五大唱片 | 佳佳唱片 | 滾石購物網 | 备注 |"
LOG_TABLE_HEAD = LOG_HEAD_ROW + "\n|---|---|---|---|---|\n"


def _log_cells(line: str) -> list[str] | None:
    s = line.strip()
    if not s.startswith("|"):
        return None
    return [c.strip() for c in s.strip("|").split("|")]


def migrate_log(content: str) -> str:
    """老日志只有两列状态。补上滾石列（旧行填「—」），顺手把表头换成新的。"""
    if LOG_HEAD_ROW in content:
        return content
    rows: list[str] = []
    for line in content.splitlines():
        cells = _log_cells(line)
        if cells is None:
            continue
        if "GMT+8" in cells[0] or cells[0] == "时间":
            continue  # 老表头行，不能当数据行
        if all(c and set(c) <= set("-: ") for c in cells):
            continue  # 分隔行
        if len(cells) == 4:  # 时间 / 五大 / 佳佳 / 备注
            cells = cells[:3] + ["—"] + cells[3:]
        rows.append("| " + " | ".join(cells) + " |")
    out = LOG_INTRO + LOG_TABLE_HEAD
    if rows:
        out += "\n".join(rows) + "\n"
    return out


def append_log(
    now: datetime,
    cur: dict,
    alerts: list[tuple[str, str]],
    note: str | None = None,
) -> None:
    if note is None:
        note = "；".join(a[1] for a in alerts) if alerts else "无变化"

    cells = [now.strftime("%Y-%m-%d %H:%M")]
    cells += [STATUS_TEXT.get(cur[k]["status"], cur[k]["status"]) for k in LOG_STATUS_KEYS]
    cells.append(note)
    row = "| " + " | ".join(cells) + " |\n"

    if not LOG_PATH.exists():
        LOG_PATH.write_text(LOG_INTRO + LOG_TABLE_HEAD + row, encoding="utf-8")
        return

    content = migrate_log(LOG_PATH.read_text(encoding="utf-8"))
    marker = "|---|---|---|---|---|\n"
    idx = content.find(marker)
    if idx == -1:  # 文件被改坏了，重建
        LOG_PATH.write_text(LOG_INTRO + LOG_TABLE_HEAD + row, encoding="utf-8")
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
    for key, name, _fn in channels():
        d = cur[key]
        url = CHANNEL_URLS[key]
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
    for key, name, _fn in channels():
        url = CHANNEL_URLS[key]
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

    for key, name, fn in channels():
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
        gh_output({"commit": "false", "changed": "false", "reason": "test-email"})
        return 0

    # 高频轮询下先确认「有货」不是页面抖动
    confirm_in(prev, cur)
    cur["ship_date"] = (cur.get("five") or {}).get("ship_date")

    alerts = decide_alerts(prev, cur)
    alerts, alerted = apply_cooldown(alerts, prev, now)
    print(f"  变化：{alerts if alerts else '无'}")

    need_mail = bool(alerts) or force_email
    any_in = any(cur[k]["status"] == "in" for k, _n, _f in channels())

    backups = []
    if any_in:
        print("  检测到有货，顺带扫描备用通路…")
        backups = check_backups()
        for b in backups:
            print(f"    {b['name']}: {b['state']}")

    mail_ok = True
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
                mail_ok = False
                print(f"  邮件发送失败：{exc}")
                print(text)
    else:
        print("  无变化，不发送邮件")

    # ---------- 决定这一次要不要写日志 / 提交 ----------
    # 每分钟跑一次，若每次都写，一天就是 1440 个 commit。
    # 所以：有变化 → 写（不写下一分钟会重复发信）；无变化 → 今天第一次运行时写一次快照。
    today = now.strftime("%Y-%m-%d")
    snapshot_due = prev.get("last_write_date") != today
    force_write = "--force-write" in args or os.getenv("FORCE_WRITE", "").lower() in ("1", "true", "yes")
    changed = bool(alerts)

    if not mail_ok:
        write_repo = False
        reason = "邮件发送失败，保留旧状态等下一轮重试"
    elif changed:
        write_repo = True
        reason = "状态有变化"
    elif snapshot_due:
        write_repo = True
        reason = "每日快照"
    elif force_write:
        write_repo = True
        reason = "手动强制写入"
    else:
        write_repo = False
        reason = "无变化且今日已写过，不碰仓库"

    print(f"  仓库写入：{'是' if write_repo else '否'}（{reason}）")

    if dry_run:
        print("（dry-run：不写入 state.json / monitor_log.md）")
        gh_output(
            {
                "commit": False,
                "changed": changed,
                "reason": "dry-run",
                "five": cur["five"]["status"],
                "ccr": cur["ccr"]["status"],
                "rock": cur["rock"]["status"],
            }
        )
        return 0

    if write_repo:
        append_log(now, cur, alerts, note=None if changed else "无变化（每日快照）")
        save_state(
            {
                "last_run": now.isoformat(timespec="seconds"),
                "last_write_date": today,
                "five": cur["five"],
                "ccr": cur["ccr"],
                "rock": cur["rock"],
                "ship_date": cur["ship_date"],
                "alerted": alerted,
            }
        )
        print("  已更新 monitor_log.md 与 state.json")

    gh_output(
        {
            "commit": write_repo,
            "changed": changed,
            "reason": reason,
            "five": cur["five"]["status"],
            "ccr": cur["ccr"]["status"],
        }
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"未捕获异常：{exc}", file=sys.stderr)
        raise SystemExit(1)
