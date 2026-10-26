#!/usr/bin/env python3
"""
财经网站手动抓取一次 -> 生成 Markdown 简报 -> PushPlus 推送到微信。

数据源（按需求清单，已验证可抓取类）：
  华尔街见闻 / 第一财经 / 界面新闻 / 21世纪经济报道 / CNBC / Reuters
  （其余：财新公开页、Bloomberg 公开页、巨潮资讯、SEC EDGAR、雪球公开文、
   Seeking Alpha、Substack —— 在 SOURCES 里已注释列出，自行启用即可。
   付费墙文章无登录态只能抓到预览段落，属预期行为。）

抓取方式：
  1. 设置了 FIRECRAWL_API_KEY -> 走 Firecrawl REST v2 /scrape（markdown）
  2. 没设置 -> 直抓网页 HTML 并抽取标题链接（兜底，效果弱于 Firecrawl）

推送方式：
  PushPlus  http://www.pushplus.plus/send  (template=markdown, channel=wechat)

用法：
  cp .env.example .env   # 填 FIRECRAWL_API_KEY(可选) + PUSHPLUS_TOKEN(推微信必填)
  python3 finance_scrape_pushplus.py --dry-run          # 只抓+生成简报，不推送
  python3 finance_scrape_pushplus.py --push             # 抓取并推送到微信
  python3 finance_scrape_pushplus.py --push --per-source 8 --only wallstreetcn,cnbc

仅依赖 Python 标准库，无需 pip install。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).resolve().parent
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# key -> (显示名, 入口URL)。入口用列表页/频道页：一次抓取即可拿到当日头条链接。
SOURCES: dict[str, tuple[str, str]] = {
    "wallstreetcn": ("华尔街见闻", "https://wallstreetcn.com/"),
    "yicai": ("第一财经", "https://www.yicai.com/"),
    "jiemian": ("界面新闻", "https://www.jiemian.com/"),
    "21jingji": ("21世纪经济报道", "https://www.21jingji.com/"),
    "cnbc": ("CNBC Finance", "https://www.cnbc.com/finance/"),
    "reuters": ("Reuters Finance", "https://www.reuters.com/business/finance/"),
    # ---- 清单内其余站点，按需取消注释（付费墙/强反爬成功率不保证）----
    # "caixin": ("财新网(公开页)", "https://www.caixin.com/"),
    # "bloomberg": ("Bloomberg(公开页)", "https://www.bloomberg.com/markets"),
    # "cninfo": ("巨潮资讯网", "https://www.cninfo.com.cn/"),
    # "sec_edgar": ("SEC EDGAR", "https://www.sec.gov/edgar/search/"),
    # "xueqiu": ("雪球(公开文)", "https://xueqiu.com/"),
    # "seekingalpha": ("Seeking Alpha", "https://seekingalpha.com/"),
}

BAD_TITLE_HINTS = (
    "登录", "注册", "下载", "广告", "隐私", "协议", "关于我们", "联系我们",
    "login", "sign in", "subscribe", "advertisement", "privacy", "cookie",
)


# ---------------------------------------------------------------- env
def load_env() -> None:
    """极简 .env 加载（脚本同目录），已存在的环境变量优先。"""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# ---------------------------------------------------------------- http
def http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": UA, **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (固定可信域名)
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# ---------------------------------------------------------------- scrape
def firecrawl_scrape_markdown(url: str, api_key: str, api_url: str) -> str:
    """Firecrawl v2 /scrape -> markdown。注意：v1 的 FirecrawlApp.scrape_url(params=...) 已废弃。"""
    data = http_post_json(
        f"{api_url.rstrip('/')}/v2/scrape",
        {"url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": 60000},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=90,
    )
    if not data.get("success"):
        raise RuntimeError(f"Firecrawl 返回失败: {data.get('error') or data}")
    return (data.get("data") or {}).get("markdown") or ""


class _AnchorParser(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.links, self._href, self._text = base, [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            text = " ".join("".join(self._text).split())
            if text:
                self.links.append((text, urljoin(self.base, self._href)))
            self._href, self._text = None, []


def extract_from_markdown(md: str, base_url: str) -> list[tuple[str, str]]:
    links = []
    for text, url in re.findall(r"\[([^\]]{2,200})\]\((https?://[^)\s]+)\)", md):
        text = html.unescape(re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)).replace("\\", " ").strip()
        text = " ".join(text.split())
        links.append((text, url))
    if not links:  # markdown 里没链接时退化为标题行
        for line in md.splitlines():
            t = line.strip(" #*-").strip()
            if len(t) >= 10:
                links.append((t, base_url))
    return links


def pick_headlines(links: list[tuple[str, str]], base_url: str, per_source: int) -> list[tuple[str, str]]:
    host = urlparse(base_url).netloc.replace("www.", "")
    seen: set[str] = set()
    picked: list[tuple[str, str]] = []
    for text, url in links:
        if len(text) < 8 or len(text) > 110:
            continue
        if any(h in text.lower() for h in BAD_TITLE_HINTS):
            continue
        link_host = urlparse(url).netloc.replace("www.", "").replace("m.", "")
        if host and link_host and host not in link_host and link_host not in host:
            continue  # 只要本站文章，去掉外链/导航
        key = re.sub(r"\W+", "", text)[:40]
        if key in seen:
            continue
        seen.add(key)
        picked.append((text, url))
        if len(picked) >= per_source:
            break
    return picked


def scrape_source(name: str, url: str, per_source: int, api_key: str, api_url: str) -> tuple[list[tuple[str, str]], str]:
    """返回 (头条列表, 实际抓取方式)。"""
    if api_key:
        md = firecrawl_scrape_markdown(url, api_key, api_url)
        return pick_headlines(extract_from_markdown(md, url), url, per_source), "firecrawl"
    raw = http_get_text(url)
    parser = _AnchorParser(url)
    parser.feed(raw)
    return pick_headlines(parser.links, url, per_source), "direct(无Firecrawl Key兜底)"


# ---------------------------------------------------------------- push
def pushplus_send(token: str, title: str, content_md: str, topic: str = "") -> dict:
    payload = {"token": token, "title": title, "content": content_md,
               "template": "markdown", "channel": "wechat"}
    if topic:
        payload["topic"] = topic
    return http_post_json("http://www.pushplus.plus/send", payload, timeout=30)


# ---------------------------------------------------------------- main
def build_digest(results: list[dict], now: dt.datetime) -> str:
    lines = [f"# 财经简报 {now:%Y-%m-%d %H:%M}", ""]
    for r in results:
        lines.append(f"## {r['name']}（{r['mode']}）")
        if r["error"]:
            lines.append(f"- ⚠️ 抓取失败：{r['error']}")
        elif not r["items"]:
            lines.append("- ⚠️ 抓到页面但未抽出头条（可能付费墙/反爬/结构变化）")
        for i, (text, url) in enumerate(r["items"], 1):
            lines.append(f"{i}. [{text}]({url})")
        lines.append("")
    lines.append("---")
    lines.append("来源：上述各站公开页面 | 工具：Firecrawl/直抓兜底 | 推送：PushPlus 微信")
    lines.append("注：付费墙文章仅标题/预览；实时行情请走行情 API，勿抓 DOM。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="财经站点抓取一次并 PushPlus 推微信")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只抓取生成简报，不推送")
    mode.add_argument("--push", action="store_true", help="抓取并 PushPlus 推送到微信")
    ap.add_argument("--per-source", type=int, default=5, help="每站头条条数（默认5）")
    ap.add_argument("--only", default="", help="只抓指定站点，逗号分隔，如 wallstreetcn,cnbc")
    ap.add_argument("--title", default="", help="PushPlus 标题（默认：财经简报 YYYY-MM-DD）")
    ap.add_argument("--max-push-chars", type=int, default=6000, help="推送正文截断上限（微信长度限制）")
    args = ap.parse_args()

    load_env()
    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    api_url = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev").strip()
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    topic = os.environ.get("PUSHPLUS_TOPIC", "").strip()

    if args.push and not token:
        print("❌ 未设置 PUSHPLUS_TOKEN，无法推送。请在 .env 填入（pushplus.plus 登录后获取，微信扫码关注公众号）。", file=sys.stderr)
        return 2
    if not api_key:
        print("ℹ️ 未设置 FIRECRAWL_API_KEY，使用直抓兜底（效果弱于 Firecrawl，建议填 Key）。")

    selected = [k.strip() for k in args.only.split(",") if k.strip()] or list(SOURCES)
    unknown = [k for k in selected if k not in SOURCES]
    if unknown:
        print(f"❌ 未知站点 {unknown}，可选：{list(SOURCES)}", file=sys.stderr)
        return 2

    now = dt.datetime.now()
    results = []
    for key in selected:
        name, url = SOURCES[key]
        print(f"抓取 {name} {url} ...")
        try:
            items, how = scrape_source(name, url, args.per_source, api_key, api_url)
            results.append({"name": name, "url": url, "mode": how, "items": items, "error": ""})
            print(f"  ✓ {how}，抽到 {len(items)} 条")
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as exc:
            results.append({"name": name, "url": url, "mode": "-", "items": [], "error": str(exc)[:200]})
            print(f"  ✗ 失败：{exc}")

    digest = build_digest(results, now)
    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"finance_digest_{now:%Y%m%d_%H%M}.md"
    out_file.write_text(digest, encoding="utf-8")
    print(f"\n简报已保存：{out_file}\n")
    print(digest)

    if args.dry_run:
        print("\n(--dry-run：未推送)")
        return 0 if any(r["items"] for r in results) else 1

    title = args.title or f"财经简报 {now:%Y-%m-%d}"
    body = digest if len(digest) <= args.max_push_chars else digest[: args.max_push_chars] + "\n\n…（已截断，完整版见 output/ 简报文件）"
    try:
        resp = pushplus_send(token, title, body, topic)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"❌ PushPlus 请求失败（网络/接口）：{exc}", file=sys.stderr)
        return 1
    print(f"\nPushPlus 响应：{json.dumps(resp, ensure_ascii=False)}")
    if resp.get("code") == 200:
        print("✅ 已推送，请查看微信（PushPlus 公众号消息）。流水号见上 data 字段。")
        return 0
    print("❌ PushPlus 推送未成功，请按响应 msg 排查（token/关注公众号/额度）。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
