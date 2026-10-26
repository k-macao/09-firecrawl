# 财经资讯一次性抓取 → PushPlus 微信推送

把《Firecrawl 在财经与金融领域的应用》里点名的那批财经站点**手动抓一次**，用 Firecrawl 的
`markdown` + 结构化 `json` 提取拿到干净条目，拼成一份摘要，通过 **PushPlus** 推到微信。

跑一次抓一次、推一次 —— 没有定时任务、没有队列、没有守护进程。

```
财经站点 ──Firecrawl scrape(markdown + json extract)──▶ 结构化条目 ──▶ 摘要渲染 ──▶ PushPlus /send ──▶ 微信
```

---

## 快速开始

```bash
cd examples/finance-digest-pushplus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # 填 FIRECRAWL_API_KEY 和 PUSHPLUS_TOKEN

python finance_digest.py --dry-run     # ① 先干跑：只抓 + 落盘，不推微信
open output/preview.html               # ② 肉眼核对微信里会长什么样
python finance_digest.py               # ③ 确认没问题，正式推送
```

> **PushPlus 前置条件**：到 <https://www.pushplus.plus/> 微信扫码登录，复制首页的「用户 token」，
> 并**关注公众号「pushplus推送加」**——没关注的话 `channel=wechat` 发不出去。

---

## 抓取源

源清单在 [`sources.json`](./sources.json)，按原文分成三类，**14 个源默认全部启用**（含付费墙的财新 / Bloomberg / 迈博汇金）：

| 分类 | 源 |
| --- | --- |
| **一、财经新闻与主流媒体**（9） | 华尔街见闻（7x24 快讯 / 深度资讯）、第一财经、界面新闻、21 世纪经济报道、财新网、Reuters Markets、CNBC World Markets、Bloomberg Markets |
| **二、财报、研报与公告**（3） | 巨潮资讯网（最新公告）、SEC EDGAR（Latest 10-Q）、迈博汇金 |
| **三、博客、独立分析与社区**（2） | 雪球（今日话题）、Seeking Alpha（Market News） |

付费墙源也开着——它们能抓到多少免费内容，`sources.json` 里每个源的 `note` 字段写了实测结论，
下面「财经抓取的边界」一节也有汇总。想临时关掉某个源就改 `enabled`，或只抓指定几个：

```bash
python finance_digest.py --only wallstreetcn-live,yicai-news
python finance_digest.py --only caixin,bloomberg-markets --dry-run
```

### 每个源的抓取参数怎么定

`sources.json` 里每个源都可以单独配 Firecrawl 的 `scrape` 选项，脚本原样透传：

| 字段 | 作用 | 财经站的实战取值 |
| --- | --- | --- |
| `wait_for` | 等 JS 渲染的毫秒数 | SPA（华尔街见闻 / 雪球）给 `4000`；服务端渲染（第一财经 / 巨潮）给 `1500~3000`；纯静态（SEC EDGAR）给 `0` |
| `actions` | 模拟滚动/点击 | 无限滚动的快讯流必须加 2~3 次 `scroll` + `wait`，否则只拿到首屏 |
| `proxy` | 代理档位 | 普通站 `basic`；有反爬的 `auto`；强 Cloudflare（CNBC / Bloomberg）`stealth` |
| `only_main_content` | 剥离侧边栏/推荐位 | 一律 `true`——这正是文中说的「剥离右侧侧边栏、推荐广告」 |
| `block_ads` | 屏蔽广告 | 资讯站开 `true` |
| `location` | 出口地区与语言 | 英文源给 `{"country":"US","languages":["en-US"]}` |
| `timeout` | 单源超时 | 渲染重的给 `90000` |
| `limit` | 最多抽几条 | 快讯 `20`，公告 `25` |

失败时脚本会**自动沿 `basic → auto → stealth` 升档重试**（`--retries` 控制次数），
专门对付文中提到的「带强 Cloudflare 防火墙的平台」。

### 结构化提取

抽取用的是 Firecrawl v2 的 `json` format + Pydantic schema，schema 就是文中
`FinancialArticle` 的列表页版本（[`finance_digest.py`](./finance_digest.py) 里的 `NewsItem`）：

```python
class NewsItem(BaseModel):
    title: str                      # 标题，快讯无标题时用正文首句概括
    url: str                        # 详情页绝对链接
    published_at: str               # 页面显示的原始时间文本
    summary: str                    # 一句话要点
    company_name: str               # 涉及的公司
    stock_code: str                 # 股票代码，保留 SH/SZ 前缀
    key_metrics: Dict[str, str]     # {"revenue": "29.97亿元", "q3_net_profit_yoy": "+143.77%"}
```

`prompt` 里明确写了「**严禁编造页面上不存在的数字或标题**」，导航/广告/页脚/二维码一律忽略。
中文源走中文 prompt、英文源走英文 prompt（`pick_prompt()` 自动路由）——混用语言会明显掉抽取质量。

---

## 推送

调的是 PushPlus 官方 [`POST https://www.pushplus.plus/send`](https://www.pushplus.plus/doc/guide/api.html)：

```json
{ "token": "...", "title": "...", "content": "<div>...</div>", "template": "html", "channel": "wechat" }
```

判定成功看响应体的 `code == 200`（HTTP 200 不代表发送成功）。

### 微信正文限长

微信渠道正文过长会被截断甚至发送失败，而且**中文 1 字 = 3 字节**，按字符数估算会严重低估 payload。
所以限长按 **UTF-8 字节**算（`--max-bytes`，默认 `24000`），并采用两级降载：

1. **先减每源条数** —— 逐级下调「每源显示前 N 条」，让每个源都还出现在推送里，
   区块标题自动变成「（20 条，仅显示前 5 条）」，读者知道被裁过；
2. **再整源丢弃** —— 条数压到最低仍超预算，才从后往前丢掉整个源区块，并在正文末尾列出被省略的源。

全程以完整标签块为粒度取舍，**绝不从标签中间切断**，产出的 HTML 始终闭合良好。
不想裁就调大上限：

```bash
python finance_digest.py --max-bytes 60000
```

### 其他推送姿势

```bash
python finance_digest.py --template markdown          # 改用 markdown 模板
python finance_digest.py --topic my-finance-group     # 一对多，推给整个群组
python finance_digest.py --to friend1,friend2         # 好友消息
python finance_digest.py --channel wechat,mail        # 多渠道（需配 --option）
python finance_digest.py --title "早报 10/26"          # 自定义标题
```

---

## 产物

每次跑完在 `output/` 落四个文件（按东八区日期命名）：

| 文件 | 内容 |
| --- | --- |
| `digest-<日期>.json` | 结构化原始数据。含每源的 `ok`/`error`/`status_code`/`proxy_used`/`scrape_id`/耗时 |
| `digest-<日期>.md` | Markdown 摘要，可直接进 RAG 知识库或塞给 LLM |
| `digest-<日期>.html` | **完整版** HTML 正文 |
| `digest-<日期>.pushed.html` | **微信实际会收到的**正文（已限长） |
| `preview.html` | 手机外壳预览页，浏览器打开就能核对推送效果 |

已经抓过一次、只想改标题或改预算重推，不必再花 Firecrawl 额度：

```bash
python finance_digest.py --from-json output/digest-2026-10-26.json --title "财经晚报"
```

---

## 财经抓取的边界（实测记录）

`output/digest-2026-10-26.json` 里如实记录了本次每个源的成败，几条经验：

- **SPA 不加 `wait_for` + 滚动 = 抓了个寂寞。** 华尔街见闻 `/news/global`、`/live/global` 直连只返回
  「正在加载」骨架和 App 下载二维码。这是文中「调整 Wait for / 增加 Actions 模拟滚动」最直接的场景。
- **别硬编码频道页路径。** 21 世纪经济报道的 `/channel/latestnews` 已经 404。财经站改版频繁，
  生产上应先用 Firecrawl 的 `/map` 或 `/crawl` 发现当前有效路径，再把稳定 URL 写回 `sources.json`。
- **强 Cloudflare 会直接 5xx。** CNBC `/world-markets/` 本次返回 HTTP 500，需要把 `proxy` 升到 `stealth`。
- **付费墙≠什么都抓不到，要看锁在哪一层。** 本次实测三个付费墙源：
  - **财新**：请求 `www.caixin.com` 会 302 到英文站 `caixinglobal.com`，首页 TOP STORIES / INSIGHT /
    LATEST NEWS 的**标题 + 摘要段（dek）+ 详情深链全部公开可读**（拿到 13 条），墙锁的是正文。
    结论：列表页够做摘要；要中文财新正文得带登录 Cookie（Firecrawl 的 `headers` 选项）。
    ⚠️ 因为发生了跨语言重定向，这个源在 `sources.json` 里必须显式写 `"language": "en"`，
    否则按域名判断会套用中文 prompt，抽取质量明显下降。
  - **Bloomberg**：`/markets` 这个 **hub 页公开可读**（拿到 14 条）——Top Securities 行情条
    （SPX/CCMP/B500/US10Y/Crude/FTSE/Gold/EURUSD/GBPUSD）和十几条带深链的头条都完整取到，
    只有 Markets Daily newsletter 区块标着 `Subscriber Only`。抓 hub 页性价比高，点进单篇深度文才会撞墙。
  - **迈博汇金**：`www.mbhj.com` 直接 `ERR_BLOCKED_BY_CLIENT`，域名已失效或被拦截。
    这暴露了研报聚合站的现实风险：**域名极不稳定**，不要把聚合站硬编码进定时任务，
    改用各券商官网的公开研报展示页，并先用 `/map` 确认路径。
  - 文中点名的 WSJ / FT 同理：没有登录 Cookie 时只能取到开头免费预览段。
- **实时行情不要用 Firecrawl 抓 DOM。** 毫秒级分时/K 线（同花顺、东方财富）请直连它们的
  WebSocket / RESTful 接口。Firecrawl 的强项是「把文章和公告变成干净的 Markdown / JSON」，不是 tick data。
- **公告列表页是 extract 的理想输入。** 巨潮资讯网的「最新公告」是标准 HTML `<table>`，
  代码/简称/标题/日期四列完整转成 Markdown 表格，每行还带 `announcementId` 详情深链——
  顺着深链再 scrape 一次 10-Q / 三季报正文，就能抽营收、净利、资产负债表核心指标。
- **源越多，微信里每源条数越少。** 14 个源、88 条时，24000 字节的默认预算下每源只剩前 3 条。
  要么调大 `--max-bytes`，要么用 `--only` 只推关心的几个源，要么拆成早/晚两次推送。

### 本次实跑结果（2026-10-26）

`output/digest-2026-10-26.json` 完整记录了 14 个源的成败：

| 结果 | 源 | 条数 |
| --- | --- | --- |
| ✅ 8 个成功 | 华尔街见闻快讯 7 ｜ 第一财经 13 ｜ 财新 13 ｜ Reuters 11 ｜ Bloomberg 14 ｜ 巨潮 10 ｜ SEC EDGAR 8 ｜ 雪球 12 | **88** |
| ❌ 6 个失败 | 华尔街见闻深度（SPA 未渲染）、21 世纪（404）、CNBC（500）、迈博汇金（域名失效）、界面新闻 / Seeking Alpha（本次未覆盖） | 0 |

---

## 自检

不花额度、不联网，验证解析、语言路由、JSON 往返、限长不变式和 PushPlus 请求体：

```bash
python selftest.py       # 63 项断言
```

覆盖的是那些「跑一次真实抓取要花额度、错了才知道」的逻辑，包括：
限长后 HTML 标签必须闭合、分类标题永不被丢弃、`<script>` 必须转义、
请求体字段名必须与官方接口一致。

---

## 文件

```
examples/finance-digest-pushplus/
├── finance_digest.py     一次性抓取 → 渲染 → 推送
├── sources.json          财经源清单 + 每源的 Firecrawl 抓取参数 + 中英文 extract prompt
├── selftest.py           离线自检（63 项）
├── requirements.txt
├── .env.example
└── output/               产物（含一份真实抓取的示例摘要）
```
