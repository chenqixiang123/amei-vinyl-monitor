# 偷故事的人 黑胶 · 补货监控（云端版）

张惠妹《偷故事的人》黑胶（1LP，编号 6710340，条码 0602567103400，EMI／环球，2026 再版定价 NT$1,840）在两家台湾唱片行的补货监控，跑在 GitHub Actions 上，**不需要你自己的电脑开机**。

监控对象：

| 通路 | 页面 | 无货特征 |
|---|---|---|
| 五大唱片 | https://www.5music.com.tw/CDList-C.asp?cdno=439405678604 | 「(目前無現貨)」 |
| 佳佳唱片 | https://www.ccr.com.tw/goods/357085 | 「無庫存」 |

有货 → 立刻发邮件；无变化 → 静默，不刷屏。每天台湾时间 **10:00 / 12:00** 各跑一次。

---

## 部署（大约 10 分钟）

### 第 1 步：建仓库

在 GitHub 新建一个 **Private** 仓库（比如 `amei-vinyl-monitor`），然后把本目录的全部内容传上去。两种方式任选：

**A. 网页上传（不用装任何东西）**

1. 打开新仓库，点「Add file」→「Upload files」
2. 把本文件夹里的 `.github`、`monitor.py`、`tests`、`README.md` 一起拖进去（Chrome/Edge 支持直接拖文件夹）
3. 点「Commit changes」

> 注意：`.github` 是隐藏文件夹，拖拽时确保它被带上了。上传后仓库首页应该能看到 `.github/workflows/monitor.yml`。

**B. 命令行**

```bash
cd cloud-monitor
git init
git add .
git commit -m "init: 补货监控"
git branch -M main
git remote add origin https://github.com/<你的用户名>/amei-vinyl-monitor.git
git push -u origin main
```

### 第 2 步：配置邮件

在仓库页面：**Settings → Secrets and variables → Actions → New repository secret**，依次添加：

| Secret 名称 | 填什么 | 示例 |
|---|---|---|
| `SMTP_HOST` | 邮箱的 SMTP 服务器 | `smtp.qq.com` |
| `SMTP_PORT` | 端口，**必须 465 或 587** | `465` |
| `SMTP_USER` | 发件邮箱完整地址 | `yourname@qq.com` |
| `SMTP_PASS` | **授权码**，不是登录密码 | `abcdwxyz12345678` |
| `MAIL_TO` | 收件地址，多个用英文逗号分隔；不填则发给 SMTP_USER | `yourname@qq.com` |

常见邮箱参数：

| 邮箱 | SMTP_HOST | 端口 | 授权码在哪拿 |
|---|---|---|---|
| QQ 邮箱 | `smtp.qq.com` | 465 | 设置 → 账号 → 开启 SMTP 服务 → 生成授权码 |
| 163 / 126 | `smtp.163.com` | 465 | 设置 → POP3/SMTP/IMAP → 开启 → 新增授权密码 |
| Gmail | `smtp.gmail.com` | 465 | 需先开两步验证 → 应用专用密码 |
| Outlook | `smtp.office365.com` | 587 | 账户安全 → 应用密码 |

> ⚠️ **不要用 25 端口**。GitHub 的服务器（Azure）封了出站 25 端口，一定会超时。

### 第 3 步：启用并试跑

1. 仓库页面 → **Actions** 标签 → 如果看到提示就点「I understand my workflows, go ahead and enable them」
2. 左侧选「偷故事的人 黑胶补货监控」→ 右侧「Run workflow」
3. 勾上 `dry_run` 先跑一次，看日志里两家的状态判断对不对（不会发信、不会改状态）
4. 再跑一次不勾 `dry_run` 的，确认没报错。之后它会按时间自动跑

---

## 本地先试一遍（可选，推荐）

不需要任何第三方库，Python 3.9+ 即可：

```bash
cd cloud-monitor

# 只看结果，不发信、不写文件
python monitor.py --dry-run

# 跑单元测试（验证页面解析和报警逻辑）
python tests/test_parse.py

# 验证邮件配置（先设置环境变量）
# Windows PowerShell:
$env:SMTP_HOST="smtp.qq.com"; $env:SMTP_PORT="465"
$env:SMTP_USER="you@qq.com"; $env:SMTP_PASS="授权码"; $env:MAIL_TO="you@qq.com"
python monitor.py --test-email
```

dry-run 输出示例：

```
[2026-11-16 18:44:48] 开始检查
  五大唱片: oos  价格=NT$1,840  出货=2026-10-02
  佳佳唱片: oos  价格=NT$1,840  出货=None
  变化：无
  无变化，不发送邮件
```

---

## 判断逻辑（为什么不会误报）

- **五大**：出现「(目前無現貨)」＝无货；该字样消失**且**出现购物车按钮＝有货；两者都没有＝`unknown`，会发一封「需人工确认」的提醒，而不是当成有货。
- **佳佳**：只认页面可见文字，**不认**页面里的 schema.org `InStock` 标记——那个标记是常年写死的，会骗人（现在页面明明写着「無庫存」，JSON-LD 里却是 `InStock`）。
- **出货日期**：单独跟踪五大的「出貨日期」。这个日期被改成新的＝第二批开放预购的最早信号，比页面变“有货”更早。
- **去重**：状态没变就不发信。只有「无货→有货」「出货日期变化」「页面改版」「连续两轮抓取失败」才发。
- **失败保护**：单次抓取失败不报警（有 3 次重试），连续两轮两家都失败才提醒，避免半夜网络抖动把你吵醒。

---

## 常见问题

**定时会准时吗？**
不会完全准时。GitHub 的定时任务是排队执行的，高峰期通常延迟 5–20 分钟，偶尔会跳过一班。对补货监控够用，但要抢现货的话，别指望它比人快。

**免费额度够吗？**
公开仓库的 Actions 完全免费无限；私有仓库每月 2000 分钟，本任务每次跑约 20–40 秒，一天两次，一个月不到 30 分钟，绰绰有余。

**会不会跑一段时间自己停掉？**
定时任务会在仓库连续 60 天没有任何提交时被自动停用；GitHub 也会在仓库长期无活动时提醒。本脚本每次运行都会更新并提交 `state.json`，等于自动保活。如果哪天真被停用了，进 Actions 页面点一下「Enable workflow」即可。

**想改检查时间？**
编辑 `.github/workflows/monitor.yml` 里的 `cron`。GitHub 的 cron 用 **UTC**：

| 台湾时间 | cron（UTC） |
|---|---|
| 10:00 | `0 2 * * *` |
| 12:00 | `0 4 * * *` |
| 09:00–23:00 每 30 分钟 | `*/30 1-15 * * *` |
| 每小时 | `0 * * * *` |

当前配置是 `0 2,4 * * *`（10:00 与 12:00 各一次）。

**想加通路？**
在 `monitor.py` 里参照 `parse_five` / `parse_ccr` 加一个解析函数，再在 `main()` 的循环里加一项即可。备用通路（風谷、滾石、老頭音樂、小白兔、博客來）已经在 `BACKUPS` 里，只在主监控出现有货时才会被扫描，结果附在邮件末尾。

**会不会有安全风险？**
授权码只存在 GitHub Secrets 里，不会写进代码或日志。仓库建议设为 Private。如果哪天想撤销，去邮箱后台重置授权码即可，本任务会随之失效。

---

## 文件结构

```
cloud-monitor/
├── .github/workflows/monitor.yml   # 定时任务定义
├── monitor.py                      # 监控主程序（纯标准库，无依赖）
├── tests/test_parse.py             # 页面解析与报警逻辑的单元测试
├── state.json                      # 上一次的状态（自动生成／自动提交）
├── monitor_log.md                  # 每次检查的历史记录（自动追加）
└── README.md
```

`monitor_log.md` 里的每一行就是一次检查结果，想回溯“到底哪天才开始缺货”直接翻这个文件。

---

## 和本地 WorkBuddy 自动化的关系

本方案与本地那两条自动化（10:00 / 12:00）监控的是同一批页面、同一套判断标准。两者可以并存：本地那条作为补充（电脑开着时延迟更低），云端这条保证你关机也有记录和通知。

如果只想留一套，建议停掉本地那两条，避免同一天收到两封重复邮件。
