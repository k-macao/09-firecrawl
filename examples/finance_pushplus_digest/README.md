# 财经站点抓取 → PushPlus 微信简报

按财经站清单手动抓一次：华尔街见闻、第一财经、界面新闻、21世纪经济报道、CNBC、Reuters
（脚本 `SOURCES` 里另有财新公开页、Bloomberg 公开页、巨潮资讯、SEC EDGAR、雪球、Seeking Alpha
等注释项，按需启用）。抓取结果整理为 Markdown 简报，经 PushPlus 推到微信。

本次手动实抓样例见 [`sample_manual_2026-10-26.md`](sample_manual_2026-10-26.md)。

## 快速开始

```bash
cd examples/finance_pushplus_digest
cp .env.example .env        # 填 PUSHPLUS_TOKEN（必填）、FIRECRAWL_API_KEY（推荐）
python3 finance_scrape_pushplus.py --dry-run   # 先只抓不推，确认简报内容
python3 finance_scrape_pushplus.py --push      # 抓取并推送到微信
```

- 仅依赖 Python 3.11+ 标准库，无需 `pip install`。
- `.env`：`PUSHPLUS_TOKEN` 在 <https://www.pushplus.plus> 登录后获取，并用微信扫码关注其公众号；
  `FIRECRAWL_API_KEY` 在 <https://firecrawl.dev> 获取。不填 Key 也能跑（直抓兜底），但抽取质量弱于 Firecrawl。
- 可选参数：`--per-source 8`、`--only wallstreetcn,cnbc`、`--title "盘前简报"`。完整简报存 `output/`（已 gitignore）。

## GitHub Actions 手动触发（不想在本机跑时用）

1. 仓库 Settings → Secrets and variables → Actions，添加 `PUSHPLUS_TOKEN`、`FIRECRAWL_API_KEY`。
2. Actions 页选择 **Finance PushPlus Digest** → Run workflow（可勾 dry_run 只生成不推送）。
3. 需要定时的话，取消 `.github/workflows/finance-pushplus.yml` 里 `schedule` 的注释。

## 注意事项

- **写法纠正**：流传的 `FirecrawlApp(api_key=...).scrape_url(url, params={'formats':['extract'], ...})`
  是已废弃的 v1 写法。现行 v2 为 `Firecrawl(api_key=...).scrape(url, formats=['markdown'])`，
  结构化抽取用 `formats=[{"type": "json", "schema": YourModel.model_json_schema(), "prompt": "..."}]`
  （见 `apps/python-sdk/example_v2.py`）。本脚本走等价的 REST v2 `POST /v2/scrape`。
- **付费墙**：财新/WSJ/FT 等付费文无登录 Cookie 只能抓到标题与预览段落，属预期；本简报默认只用公开列表页。
- **实时行情**：分时/K 线等毫秒级数据请走行情 API，不要抓 DOM；本简报中的价格仅为抓取时刻快照。
- **PushPlus 长度**：微信模板消息正文过长会被截断，脚本推送时默认截断到 6000 字，完整版在 `output/` 与 Actions artifact 里。
- 遵守各站 robots 与服务条款，控制抓取频率（手动一次/低频定时为宜）。
