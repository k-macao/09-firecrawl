#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财经资讯一次性抓取 → PushPlus 微信推送 (one-shot finance digest → WeChat)

对应《Firecrawl 在财经与金融领域的应用》一文的三类站点：
  1. 财经新闻与主流媒体   华尔街见闻 / 第一财经 / 界面 / 21 世纪 / Reuters / CNBC
  2. 财报、研报与公告     巨潮资讯网 / SEC EDGAR
  3. 博客与知识社区       雪球 / Seeking Alpha

这是"手动跑一次"的脚本：没有定时任务、没有消息队列，执行一次就抓一次、推一次。

用法
----
    # 0) 装依赖 + 配环境变量
    pip install -r requirements.txt
    cp .env.example .env          # 填 FIRECRAWL_API_KEY / PUSHPLUS_TOKEN

    # 1) 先干跑：只抓取 + 落盘，不推微信（强烈建议第一次这么跑）
    python finance_digest.py --dry-run

    # 2) 正式推送到微信
    python finance_digest.py

    # 3) 只推昨天那份已落盘的摘要（不重新抓取，不花额度）
    python finance_digest.py --from-json output/digest-2026-10-26.json

    # 4) 只抓指定几个源
    python finance_digest.py --only wallstreetcn-live,yicai-news --dry-run

产物
----
    output/digest-<YYYY-MM-DD>.json    结构化原始数据（可用于二次推送）
    output/digest-<YYYY-MM-DD>.md      Markdown 摘要
    output/digest-<YYYY-MM-DD>.html    实际发给 PushPlus 的 HTML 正文
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import html as html_lib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # python-dotenv 是可选的
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

from pydantic import BaseModel, Field, ValidationError

HERE = Path(__file__).resolve().parent
SOURCES_PATH = HERE / "sources.json"
OUTPUT_DIR = HERE / "output"

# 北京时间，财经摘要按东八区落日期更符合直觉
CST = timezone(timedelta(hours=8), "CST")

# PushPlus 微信渠道对单条消息长度有上限，超长会被截断甚至发送失败。
# 这里按 UTF-8 字节计（中文 1 字 = 3 字节），默认值留足余量；
# 超出部分转存本地文件，并在正文末尾列出被省略的源。
DEFAULT_MAX_BYTES = 24000

PUSHPLUS_SEND_URL = "https://www.pushplus.plus/send"


# --------------------------------------------------------------------------- #
# 1. 结构化提取的数据模型（对应文中 FinancialArticle 的思路，改成列表页版本）
# --------------------------------------------------------------------------- #
class NewsItem(BaseModel):
    """一条财经快讯 / 公告 / 帖子。"""

    title: str = Field(description="标题或正文首句概括")
    url: str = Field(default="", description="详情页绝对链接，无则留空")
    published_at: str = Field(default="", description="页面显示的原始时间文本")
    summary: str = Field(default="", description="一句话要点")
    company_name: str = Field(default="", description="涉及的公司名称")
    stock_code: str = Field(default="", description="股票代码，保留交易所前缀")
    key_metrics: Dict[str, str] = Field(
        default_factory=dict,
        description="正文中明确出现的财务/市场数字，如 {'revenue': '29.97亿元'}",
    )


class NewsItemList(BaseModel):
    """LLM 抽取结果的顶层结构。"""

    items: List[NewsItem] = Field(default_factory=list)


@dataclass
class SourceSpec:
    """sources.json 里的一个抓取源。"""

    id: str
    name: str
    url: str
    category: str
    category_name: str
    enabled: bool = True
    scenario: str = ""
    note: str = ""
    wait_for: int = 0
    timeout: int = 60000
    proxy: Optional[str] = None
    only_main_content: bool = True
    block_ads: bool = False
    actions: Optional[List[Dict[str, Any]]] = None
    location: Optional[Dict[str, Any]] = None
    limit: int = 15

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], category: str, category_name: str) -> "SourceSpec":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in raw.items() if k in known and k not in ("category", "category_name")}
        kwargs["category"] = category
        kwargs["category_name"] = category_name
        return cls(**kwargs)


@dataclass
class SourceResult:
    """单个源的抓取结果（成功或失败都记录，便于诊断反爬/付费墙）。"""

    spec: SourceSpec
    ok: bool = False
    items: List[NewsItem] = field(default_factory=list)
    markdown: str = ""
    error: str = ""
    status_code: Optional[int] = None
    proxy_used: Optional[str] = None
    scrape_id: Optional[str] = None
    elapsed_s: float = 0.0
    # 实际取到内容的地址（可能与 spec.url 不同：重定向、或改用了站点的 JSON 接口）
    fetched_url: str = ""
    note: str = ""


@dataclass
class Digest:
    """一次抓取的全部产物。"""

    generated_at: str
    date_label: str
    results: List[SourceResult]
    firecrawl_api_url: str = "https://api.firecrawl.dev"
    # 数据来源标注。正常跑 Firecrawl 时为 "firecrawl"；
    # 离线复推 / 无网络环境下手工抓取时会被显式改写，避免误认为是 Firecrawl 的产物。
    collector: str = "firecrawl"

    # ---- 统计 ---- #
    @property
    def ok_sources(self) -> List[SourceResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed_sources(self) -> List[SourceResult]:
        return [r for r in self.results if not r.ok]

    @property
    def total_items(self) -> int:
        return sum(len(r.items) for r in self.results)

    def to_json(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "date_label": self.date_label,
            "firecrawl_api_url": self.firecrawl_api_url,
            "collector": self.collector,
            "stats": {
                "sources_ok": len(self.ok_sources),
                "sources_failed": len(self.failed_sources),
                "total_items": self.total_items,
            },
            "sources": [
                {
                    "id": r.spec.id,
                    "name": r.spec.name,
                    "url": r.spec.url,
                    "category": r.spec.category,
                    "category_name": r.spec.category_name,
                    "ok": r.ok,
                    "error": r.error,
                    "status_code": r.status_code,
                    "proxy_used": r.proxy_used,
                    "scrape_id": r.scrape_id,
                    "elapsed_s": round(r.elapsed_s, 2),
                    "fetched_url": r.fetched_url,
                    "note": r.note,
                    "items": [i.model_dump() for i in r.items],
                }
                for r in self.results
            ],
        }

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "Digest":
        results: List[SourceResult] = []
        for s in payload.get("sources", []):
            spec = SourceSpec(
                id=s["id"],
                name=s["name"],
                url=s["url"],
                category=s.get("category", "news"),
                category_name=s.get("category_name", ""),
            )
            items = []
            for raw in s.get("items", []):
                try:
                    items.append(NewsItem(**raw))
                except ValidationError:
                    continue
            results.append(
                SourceResult(
                    spec=spec,
                    ok=bool(s.get("ok")),
                    items=items,
                    error=s.get("error", "") or "",
                    status_code=s.get("status_code"),
                    proxy_used=s.get("proxy_used"),
                    scrape_id=s.get("scrape_id"),
                    elapsed_s=float(s.get("elapsed_s") or 0.0),
                    fetched_url=s.get("fetched_url", "") or "",
                    note=s.get("note", "") or "",
                )
            )
        return cls(
            generated_at=payload.get("generated_at", ""),
            date_label=payload.get("date_label", ""),
            results=results,
            firecrawl_api_url=payload.get("firecrawl_api_url", "https://api.firecrawl.dev"),
            collector=payload.get("collector", "firecrawl"),
        )


# --------------------------------------------------------------------------- #
# 2. 加载源清单
# --------------------------------------------------------------------------- #
def load_sources(path: Path, include_disabled: bool = False) -> tuple[List[SourceSpec], Dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    specs: List[SourceSpec] = []
    for cat in data.get("categories", []):
        for raw in cat.get("sources", []):
            spec = SourceSpec.from_dict(raw, cat.get("id", "news"), cat.get("name", ""))
            if spec.enabled or include_disabled:
                specs.append(spec)
    prompts = {
        "zh": data.get("extract_prompt_zh", ""),
        "en": data.get("extract_prompt_en", ""),
    }
    return specs, prompts


def pick_prompt(prompts: Dict[str, str], spec: SourceSpec) -> str:
    """中文源用中文 prompt，英文源用英文 prompt —— 抽取质量差别很明显。"""
    host = re.sub(r"^https?://", "", spec.url).split("/")[0].lower()
    english_hosts = ("reuters.com", "cnbc.com", "bloomberg.com", "sec.gov", "seekingalpha.com", "ft.com", "wsj.com")
    key = "en" if any(host.endswith(h) for h in english_hosts) else "zh"
    return (prompts.get(key) or prompts.get("zh") or "").replace("{limit}", str(spec.limit))


# --------------------------------------------------------------------------- #
# 3. Firecrawl 抓取（scrape + 结构化 json 提取）
# --------------------------------------------------------------------------- #
def build_firecrawl(api_key: str, api_url: str):
    """延迟导入：--from-json 复推场景下即使没装 SDK 也能跑。"""
    from firecrawl import Firecrawl  # type: ignore

    return Firecrawl(api_key=api_key, api_url=api_url or "https://api.firecrawl.dev")


def scrape_source(fc: Any, spec: SourceSpec, prompt: str, retries: int = 2) -> SourceResult:
    """抓一个源。失败时按 proxy 逐级升档重试（basic → auto → stealth）。"""
    started = time.monotonic()
    last_error = ""
    proxy_ladder = [spec.proxy or "auto"]
    if retries > 0:
        for p in ("auto", "stealth"):
            if p not in proxy_ladder:
                proxy_ladder.append(p)

    for attempt, proxy in enumerate(proxy_ladder[: retries + 1], start=1):
        try:
            kwargs: Dict[str, Any] = {
                "formats": [
                    "markdown",
                    {
                        "type": "json",
                        "prompt": prompt,
                        "schema": NewsItemList,
                    },
                ],
                "only_main_content": spec.only_main_content,
                "timeout": spec.timeout,
                "proxy": proxy,
            }
            if spec.wait_for:
                kwargs["wait_for"] = spec.wait_for
            if spec.block_ads:
                kwargs["block_ads"] = True
            if spec.actions:
                kwargs["actions"] = spec.actions
            if spec.location:
                kwargs["location"] = spec.location

            doc = fc.scrape(spec.url, **kwargs)
            meta = doc.metadata
            raw_items = (doc.json or {}).get("items", []) if isinstance(doc.json, dict) else []

            items: List[NewsItem] = []
            for raw in raw_items[: spec.limit]:
                try:
                    items.append(NewsItem(**raw))
                except ValidationError:
                    continue

            if not items and not (doc.markdown or "").strip():
                last_error = "抓取成功但内容为空（大概率是 JS 未渲染完 / 付费墙 / 反爬拦截）"
                continue

            return SourceResult(
                spec=spec,
                ok=True,
                items=items,
                markdown=doc.markdown or "",
                status_code=getattr(meta, "status_code", None),
                proxy_used=getattr(meta, "proxy_used", None) or proxy,
                scrape_id=getattr(meta, "scrape_id", None),
                elapsed_s=time.monotonic() - started,
                fetched_url=getattr(meta, "url", None) or getattr(meta, "source_url", None) or spec.url,
            )
        except Exception as exc:  # noqa: BLE001 —— 单源失败不能拖垮整批
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt <= retries:
                time.sleep(1.5 * attempt)

    return SourceResult(spec=spec, ok=False, error=last_error, elapsed_s=time.monotonic() - started)


def run_scrape(
    specs: List[SourceSpec],
    prompts: Dict[str, str],
    api_key: str,
    api_url: str,
    concurrency: int = 4,
    retries: int = 2,
) -> List[SourceResult]:
    fc = build_firecrawl(api_key, api_url)
    results: Dict[str, SourceResult] = {}
    order = [s.id for s in specs]

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(scrape_source, fc, s, pick_prompt(prompts, s), retries): s for s in specs}
        for fut in as_completed(futures):
            spec = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # pragma: no cover
                res = SourceResult(spec=spec, ok=False, error=f"{type(exc).__name__}: {exc}")
            results[spec.id] = res
            flag = "OK " if res.ok else "FAIL"
            print(f"  [{flag}] {spec.name:<28} {len(res.items):>3} 条  {res.elapsed_s:5.1f}s"
                  + (f"  proxy={res.proxy_used}" if res.proxy_used else "")
                  + (f"  ← {res.error[:90]}" if res.error else ""), flush=True)

    return [results[sid] for sid in order if sid in results]


# --------------------------------------------------------------------------- #
# 4. 渲染摘要
# --------------------------------------------------------------------------- #
def _fmt_metrics(m: Dict[str, str]) -> str:
    if not m:
        return ""
    return " · ".join(f"{k}: {v}" for k, v in list(m.items())[:6])


def _group_by_category(digest: Digest) -> Dict[str, List[SourceResult]]:
    grouped: Dict[str, List[SourceResult]] = {}
    for r in digest.results:
        grouped.setdefault(r.spec.category_name or r.spec.category, []).append(r)
    return grouped


def render_markdown(digest: Digest) -> str:
    out: List[str] = []
    out.append(f"# 财经资讯摘要 · {digest.date_label}")
    out.append("")
    origin = (
        f"Firecrawl {digest.firecrawl_api_url}"
        if digest.collector == "firecrawl"
        else f"采集方式：{digest.collector}"
    )
    out.append(
        f"> 抓取时间 {digest.generated_at} ｜ 源 {len(digest.ok_sources)}/{len(digest.results)} 成功"
        f" ｜ 共 {digest.total_items} 条 ｜ {origin}"
    )
    out.append("")

    for cat_name, results in _group_by_category(digest).items():
        out.append(f"## {cat_name}")
        out.append("")
        for r in results:
            if not r.ok:
                out.append(f"### ❌ {r.spec.name}")
                out.append(f"- 抓取失败：{r.error}")
                out.append(f"- 源地址：{r.spec.url}")
                for note in (r.note, r.spec.note):
                    if note:
                        out.append(f"- 备注：{note}")
                out.append("")
                continue

            out.append(f"### {r.spec.name} （{len(r.items)} 条）")
            if r.spec.scenario:
                out.append(f"*{r.spec.scenario}*")
            if r.fetched_url and r.fetched_url != r.spec.url:
                out.append(f"- 实际抓取地址：{r.fetched_url}")
            if r.note:
                out.append(f"- 备注：{r.note}")
            out.append("")

            if r.items:
                for i, it in enumerate(r.items, 1):
                    title = it.title.strip() or "(无标题)"
                    head = f"{i}. **{title}**"
                    if it.published_at:
                        head += f" · `{it.published_at}`"
                    out.append(head)
                    if it.summary:
                        out.append(f"   - {it.summary}")
                    tag_bits: List[str] = []
                    if it.company_name:
                        tag_bits.append(it.company_name)
                    if it.stock_code:
                        tag_bits.append(f"`{it.stock_code}`")
                    metrics = _fmt_metrics(it.key_metrics)
                    if metrics:
                        tag_bits.append(metrics)
                    if tag_bits:
                        out.append(f"   - {' ｜ '.join(tag_bits)}")
                    if it.url:
                        out.append(f"   - 原文：{it.url}")
                out.append("")
            else:
                md = (r.markdown or "").strip()
                if md:
                    # 没抽到结构化条目时，退化成 Markdown 正文摘录，至少不空手而归
                    out.append("```markdown")
                    out.append(md[:1500] + ("\n…（已截断）" if len(md) > 1500 else ""))
                    out.append("```")
                    out.append("")

    if digest.failed_sources:
        out.append("## 抓取失败与边界提示")
        out.append("")
        for r in digest.failed_sources:
            out.append(f"- **{r.spec.name}** — {r.error}")
        out.append("")
        out.append("> 付费墙（WSJ / FT / 财新深度文）无登录 Cookie 时只能取到免费预览段；")
        out.append("> 毫秒级实时行情请直连交易所/行情商的 WebSocket 或 RESTful 接口，不要用 Firecrawl 抓 DOM。")
        out.append("")

    out.append("---")
    out.append("*由 Firecrawl 抓取并结构化提取，经 PushPlus 推送到微信。*")
    return "\n".join(out)


_HTML_WRAPPER_OPEN = (
    '<div style="font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
    'font-size:14px;line-height:1.7;color:#1f2329;max-width:720px;margin:0 auto;padding:8px 4px;">'
)


def _html_head(digest: Digest) -> str:
    e = html_lib.escape
    origin = "Firecrawl" if digest.collector == "firecrawl" else e(digest.collector)
    return "\n".join([
        _HTML_WRAPPER_OPEN,
        f'<h1 style="font-size:19px;margin:0 0 4px;color:#0b1f3a;">财经资讯摘要 · {e(digest.date_label)}</h1>',
        f'<p style="margin:0 0 14px;color:#8a9099;font-size:12px;">抓取时间 {e(digest.generated_at)}'
        f" ｜ 源 {len(digest.ok_sources)}/{len(digest.results)} 成功 ｜ 共 {digest.total_items} 条"
        f" ｜ {origin}</p>",
    ])


def _html_source_block(r: SourceResult, max_items: Optional[int] = None) -> str:
    """一个源的完整 HTML 区块（自带闭合标签，可整块取舍）。

    max_items 用于限长时"每源少放几条"，比整源丢弃保留更多信息。
    """
    e = html_lib.escape
    parts: List[str] = []

    if not r.ok:
        note = r.note or r.spec.note
        note_html = f'<br/><span style="color:#8a9099;font-size:12px;">备注：{e(note)}</span>' if note else ""
        parts.append(
            f'<div style="margin:0 0 12px;padding:8px 10px;background:#fff5f5;'
            f'border-radius:6px;color:#a8071a;font-size:13px;">'
            f'<b>❌ {e(r.spec.name)}</b><br/>'
            f'<span style="color:#8a9099;">{e(r.error[:200])}</span>{note_html}</div>'
        )
        return "\n".join(parts)

    shown = r.items if max_items is None else r.items[:max_items]
    hidden = len(r.items) - len(shown)
    count_label = f"（{len(r.items)} 条）" if hidden <= 0 else f"（{len(r.items)} 条，仅显示前 {len(shown)} 条）"
    parts.append(
        f'<h3 style="font-size:14px;margin:14px 0 6px;color:#c8161d;">'
        f'{e(r.spec.name)} <span style="color:#8a9099;font-weight:normal;">{count_label}</span></h3>'
    )

    if not shown:
        snippet = (r.markdown or "").strip()[:600]
        if snippet:
            parts.append(
                f'<pre style="white-space:pre-wrap;word-break:break-all;background:#f6f7f9;'
                f'padding:8px;border-radius:6px;font-size:12px;color:#4a5058;">{e(snippet)}</pre>'
            )
        return "\n".join(parts)

    parts.append('<ol style="margin:0 0 6px;padding-left:20px;">')
    for it in shown:
        title = e(it.title.strip() or "(无标题)")
        head = (
            f'<a href="{e(it.url)}" style="color:#1f2329;text-decoration:none;">{title}</a>'
            if it.url else title
        )
        meta_html = (
            f' <span style="color:#b0b6bd;font-size:12px;">{e(it.published_at)}</span>'
            if it.published_at else ""
        )
        line = f"<li>{head}{meta_html}"
        if it.summary:
            line += f'<div style="color:#5a6169;font-size:13px;margin-top:2px;">{e(it.summary)}</div>'
        tags: List[str] = []
        if it.company_name:
            tags.append(e(it.company_name))
        if it.stock_code:
            tags.append(f'<code style="background:#f0f2f5;padding:0 4px;border-radius:3px;">{e(it.stock_code)}</code>')
        if tags:
            line += f'<div style="color:#8a9099;font-size:12px;margin-top:2px;">{" ｜ ".join(tags)}</div>'
        metrics = _fmt_metrics(it.key_metrics)
        if metrics:
            line += f'<div style="margin-top:3px;font-size:12px;color:#0b6b3a;">{e(metrics)}</div>'
        line += "</li>"
        parts.append(line)
    parts.append("</ol>")
    return "\n".join(parts)


def _html_foot(digest: Digest, dropped_note: str = "") -> str:
    parts: List[str] = []
    if dropped_note:
        parts.append(
            '<p style="margin:14px 0 0;padding:8px 10px;background:#f0f5ff;border-radius:6px;'
            f'color:#1d39c4;font-size:12px;">{dropped_note}</p>'
        )
    if digest.failed_sources:
        parts.append(
            '<p style="margin:18px 0 0;padding:8px 10px;background:#fffbe6;border-radius:6px;'
            'color:#8a6d00;font-size:12px;line-height:1.6;">'
            "⚠️ 边界提示：付费墙（WSJ / FT / 财新深度文）无登录 Cookie 只能取免费预览段；"
            "毫秒级实时行情请直连 WebSocket / RESTful 接口，不要用 Firecrawl 抓 DOM。</p>"
        )
    parts.append(
        '<p style="margin:16px 0 0;color:#b0b6bd;font-size:11px;text-align:center;">'
        "Firecrawl 抓取 + 结构化提取 ｜ PushPlus 推送微信</p>"
    )
    parts.append("</div>")
    return "\n".join(parts)


def render_preview_page(digest: Digest, title: str, pushed_html: str) -> str:
    """把「微信实际会收到的正文」套进一个手机外壳里，方便推送前肉眼核对。"""
    e = html_lib.escape
    stats = digest.to_json()["stats"]
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>PushPlus 推送预览 · {e(digest.date_label)}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:32px 16px 56px; background:#eceff3;
         font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; display:flex; gap:28px; flex-wrap:wrap;
           justify-content:center; align-items:flex-start; }}
  .side {{ flex:1 1 320px; min-width:280px; }}
  h1 {{ font-size:20px; color:#0b1f3a; margin:0 0 4px; }}
  .sub {{ color:#6b7280; font-size:13px; margin:0 0 18px; line-height:1.6; }}
  table.stats {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px;
                 overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); font-size:13px; }}
  table.stats td {{ padding:9px 12px; border-bottom:1px solid #f0f2f5; }}
  table.stats td:first-child {{ color:#6b7280; width:44%; }}
  table.stats td:last-child {{ color:#0b1f3a; font-weight:600; text-align:right; }}
  table.stats tr:last-child td {{ border-bottom:none; }}
  .box {{ margin-top:18px; background:#fff; border-radius:10px; padding:14px 16px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); font-size:13px; color:#4a5058; line-height:1.75; }}
  .box b {{ color:#0b1f3a; }}
  code {{ background:#f0f2f5; padding:1px 5px; border-radius:4px; font-size:12px; }}
  .phone {{ flex:0 0 390px; width:390px; background:#111; border-radius:44px; padding:12px;
            box-shadow:0 18px 44px rgba(0,0,0,.28); }}
  .screen {{ background:#ededed; border-radius:34px; overflow:hidden; height:780px;
             display:flex; flex-direction:column; }}
  .bar {{ background:#ededed; padding:14px 16px 10px; text-align:center; font-size:15px;
          font-weight:600; color:#111; border-bottom:1px solid #dcdcdc; flex:0 0 auto; }}
  .msg {{ flex:1 1 auto; overflow-y:auto; padding:16px 10px 28px; }}
  .bubble {{ background:#fff; border-radius:6px; padding:14px 12px;
             box-shadow:0 1px 2px rgba(0,0,0,.08); }}
  .stamp {{ text-align:center; font-size:11px; color:#9b9b9b; margin:0 0 12px; }}
</style></head>
<body>
<div class="wrap">
  <div class="side">
    <h1>PushPlus 推送预览</h1>
    <p class="sub">右侧就是 <code>channel=wechat</code> 时微信里实际收到的正文。<br/>
       本页面只是本地核对用，不会发送任何东西。</p>
    <table class="stats">
      <tr><td>摘要日期</td><td>{e(digest.date_label)}</td></tr>
      <tr><td>抓取时间</td><td>{e(digest.generated_at)}</td></tr>
      <tr><td>采集方式</td><td>{e(digest.collector)}</td></tr>
      <tr><td>成功 / 总源数</td><td>{stats['sources_ok']} / {stats['sources_ok'] + stats['sources_failed']}</td></tr>
      <tr><td>条目总数</td><td>{stats['total_items']}</td></tr>
      <tr><td>完整版正文</td><td>{_blen(render_html(digest))} 字节</td></tr>
      <tr><td>微信版正文</td><td>{_blen(pushed_html)} 字节</td></tr>
      <tr><td>推送标题</td><td style="font-weight:400">{e(title)}</td></tr>
    </table>
    <div class="box">
      <b>怎么真正发出去</b><br/>
      1. <code>cp .env.example .env</code>，填 <code>FIRECRAWL_API_KEY</code> 与 <code>PUSHPLUS_TOKEN</code><br/>
      2. 关注微信公众号「pushplus推送加」（否则 wechat 渠道发不出）<br/>
      3. <code>python finance_digest.py</code>（去掉 <code>--dry-run</code>）<br/>
      4. 只想重发这一份、不再抓取：<br/>
      &nbsp;&nbsp;&nbsp;&nbsp;<code>python finance_digest.py --from-json output/digest-{e(digest.date_label)}.json</code>
    </div>
  </div>
  <div class="phone"><div class="screen">
    <div class="bar">pushplus推送加</div>
    <div class="msg">
      <p class="stamp">{e(digest.generated_at)}</p>
      <div class="bubble">{pushed_html}</div>
    </div>
  </div></div>
</div>
</body></html>
"""


def render_html_blocks(
    digest: Digest, max_items: Optional[int] = None
) -> tuple[str, List[tuple[str, str]], str]:
    """返回 (头部, [(源名, 区块 HTML)], 尾部)。区块之间彼此独立，便于按预算整块取舍。"""
    blocks: List[tuple[str, str]] = []
    for cat_name, results in _group_by_category(digest).items():
        e = html_lib.escape
        heading = (
            f'<h2 style="font-size:16px;margin:20px 0 8px;padding-left:8px;'
            f'border-left:4px solid #c8161d;color:#0b1f3a;">{e(cat_name)}</h2>'
        )
        blocks.append((f"[分类] {cat_name}", heading))
        for r in results:
            blocks.append((r.spec.name, _html_source_block(r, max_items)))
    return _html_head(digest), blocks, _html_foot(digest)


def render_html(digest: Digest) -> str:
    """生成 PushPlus template=html 的完整正文（不限长，用于本地存档）。"""
    head, blocks, foot = render_html_blocks(digest)
    return "\n".join([head] + [b for _, b in blocks] + [foot])


def _blen(s: str) -> int:
    """UTF-8 字节数。中文占 3 字节，按字符数估算会严重低估真实 payload 体积。"""
    return len(s.encode("utf-8"))


def clamp_html(digest: Digest, max_bytes: int) -> tuple[str, bool, List[str], Optional[int]]:
    """微信渠道正文限长（按 UTF-8 字节计），两级降载：

    1. **先减每源条数**：逐级下调"每源显示前 N 条"，尽量让每个源都还出现在微信正文里，
       区块标题会自动变成「（20 条，仅显示前 8 条）」，读者知道被裁过。
    2. **再整源丢弃**：条数降到最低仍超预算时，才从后往前丢掉整个源区块。

    全程以完整标签块为粒度取舍，绝不从标签中间切断，产出的 HTML 始终闭合良好。
    返回 (html, 是否截断, 被整源丢弃的源名, 每源条数上限 or None)。
    """
    head, blocks, foot = render_html_blocks(digest)
    full = "\n".join([head] + [b for _, b in blocks] + [foot])
    if _blen(full) <= max_bytes:
        return full, False, [], None

    # ---- 第一级：压缩每源条数 ---- #
    max_items_any = max((len(r.items) for r in digest.results), default=0)
    per_source: Optional[int] = None
    for cap_items in (20, 15, 12, 10, 8, 6, 5, 4, 3, 2, 1):
        if cap_items >= max_items_any:
            continue  # 不起作用的档位直接跳过
        head, blocks, foot = render_html_blocks(digest, cap_items)
        cand = "\n".join([head] + [b for _, b in blocks] + [foot])
        if _blen(cand) <= max_bytes:
            return cand, True, [], cap_items
        per_source = cap_items

    # ---- 第二级：整源丢弃 ---- #
    head, blocks, foot = render_html_blocks(digest, per_source)
    dropped: List[str] = []
    kept: List[tuple[str, str]] = []
    # 预留尾部空间；丢弃提示的长度会随丢弃数量增长，先按经验值预留，最后再校正
    reserve = 1200
    budget = max_bytes - _blen(head) - reserve

    for label, block in blocks:
        cost = _blen(block) + 1
        if budget - cost >= 0:
            budget -= cost
            kept.append((label, block))
        elif not label.startswith("[分类]"):
            dropped.append(label)

    def _assemble(drop_list: List[str]) -> str:
        note = ""
        if drop_list:
            shown = "、".join(drop_list[:6]) + ("…" if len(drop_list) > 6 else "")
            note = (
                f"…微信正文超长，本次省略 {len(drop_list)} 个源：{shown}。"
                "完整摘要见 output/ 目录下的 .md / .html 文件。"
            )
        return "\n".join([head] + [b for _, b in kept] + [_html_foot(digest, note)])

    # 校正：提示文案变长、或 foot 比预留值更长，都可能把总量顶过上限；
    # 此时继续整块丢弃（分类标题不丢，保持版面结构）
    while _blen(_assemble(dropped)) > max_bytes:
        for idx in range(len(kept) - 1, -1, -1):
            if not kept[idx][0].startswith("[分类]"):
                label, _ = kept.pop(idx)
                dropped.insert(0, label)
                break
        else:
            break

    return _assemble(dropped), True, dropped, per_source



# --------------------------------------------------------------------------- #
# 5. PushPlus 推送
# --------------------------------------------------------------------------- #
def push_pushplus(
    token: str,
    title: str,
    content: str,
    *,
    template: str = "html",
    channel: str = "wechat",
    topic: str = "",
    to: str = "",
    option: str = "",
    timeout: int = 30,
    retries: int = 2,
) -> Dict[str, Any]:
    """调用 PushPlus 发送消息接口。返回响应 JSON；code==200 为成功。"""
    import requests  # 延迟导入，纯离线渲染时不需要

    payload: Dict[str, Any] = {
        "token": token,
        "title": title,
        "content": content,
        "template": template,
        "channel": channel,
    }
    if topic:
        payload["topic"] = topic
    if to:
        payload["to"] = to
    if option:
        payload["option"] = option

    last: Dict[str, Any] = {}
    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(PUSHPLUS_SEND_URL, json=payload, timeout=timeout)
            try:
                last = resp.json()
            except ValueError:
                last = {"code": resp.status_code, "msg": resp.text[:300]}
            if resp.status_code == 200 and last.get("code") == 200:
                return last
            print(f"  PushPlus 第 {attempt} 次返回非成功：HTTP {resp.status_code} {last}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            last = {"code": -1, "msg": f"{type(exc).__name__}: {exc}"}
            print(f"  PushPlus 第 {attempt} 次请求异常：{last['msg']}", file=sys.stderr)
        if attempt <= retries:
            time.sleep(2 * attempt)
    return last


# --------------------------------------------------------------------------- #
# 6. CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="财经资讯一次性抓取 → PushPlus 微信推送",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--sources", default=str(SOURCES_PATH), help="源清单 JSON 路径")
    p.add_argument("--only", default="", help="只抓这些源 id，逗号分隔")
    p.add_argument("--include-disabled", action="store_true", help="连 enabled=false 的付费墙源一起抓")
    p.add_argument("--dry-run", action="store_true", help="只抓取+落盘，不推微信")
    p.add_argument("--from-json", default="", help="不抓取，直接用已落盘的 digest JSON 渲染并推送")
    p.add_argument("--no-push", action="store_true", help="同 --dry-run")
    p.add_argument("--concurrency", type=int, default=4, help="并发抓取源数量，默认 4")
    p.add_argument("--retries", type=int, default=2, help="单源失败重试次数（会逐级升档 proxy）")
    p.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="微信正文最大 UTF-8 字节数，超出则按源整块省略（中文 1 字 = 3 字节）",
    )
    p.add_argument("--title", default="", help="自定义推送标题")
    p.add_argument("--template", default="html", choices=["html", "txt", "json", "markdown"], help="PushPlus 模板")
    p.add_argument("--channel", default="wechat", help="PushPlus 渠道，默认 wechat")
    p.add_argument("--topic", default="", help="PushPlus 群组编码（一对多）")
    p.add_argument("--to", default="", help="PushPlus 好友令牌（多人逗号分隔）")
    p.add_argument("--outdir", default=str(OUTPUT_DIR), help="产物输出目录")
    p.add_argument(
        "--collector",
        default="firecrawl",
        help="数据来源标注，写进摘要头部与 JSON。默认 firecrawl；离线/手工抓取时请如实改写",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    do_push = not (args.dry_run or args.no_push)

    api_key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    api_url = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev").strip()
    push_token = os.environ.get("PUSHPLUS_TOKEN", "").strip()

    # ---------- 6.1 取数据 ---------- #
    if args.from_json:
        src = Path(args.from_json)
        if not src.is_absolute():
            src = (HERE / src).resolve()
        print(f"📂 从已有摘要加载：{src}")
        digest = Digest.from_json(json.loads(src.read_text(encoding="utf-8")))
        # 未显式指定 --collector 时，沿用文件里记录的来源标注
        if args.collector != "firecrawl":
            digest.collector = args.collector
    else:
        if not api_key:
            print(
                "❌ 缺少 FIRECRAWL_API_KEY。\n"
                "   1) cp .env.example .env 并填入 key；或 2) export FIRECRAWL_API_KEY=fc-xxx\n"
                "   只想重推已有摘要，请加 --from-json output/digest-YYYY-MM-DD.json",
                file=sys.stderr,
            )
            return 2

        specs, prompts = load_sources(Path(args.sources))
        if args.only:
            wanted = {s.strip() for s in args.only.split(",") if s.strip()}
            specs = [s for s in specs if s.id in wanted]
            missing = wanted - {s.id for s in specs}
            if missing:
                print(f"⚠️ 未找到源 id：{', '.join(sorted(missing))}（可能 enabled=false，试试 --include-disabled）",
                      file=sys.stderr)
        if not specs:
            print("❌ 没有匹配的抓取源", file=sys.stderr)
            return 2

        now = datetime.now(CST)
        print(f"🔥 Firecrawl 一次性抓取 {len(specs)} 个财经源（并发 {args.concurrency}）→ {api_url}")
        results = run_scrape(specs, prompts, api_key, api_url, args.concurrency, args.retries)
        digest = Digest(
            generated_at=now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            date_label=now.strftime("%Y-%m-%d"),
            results=results,
            firecrawl_api_url=api_url,
            collector=args.collector,
        )

    # ---------- 6.2 渲染 + 落盘 ---------- #
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    date_label = digest.date_label or datetime.now(CST).strftime("%Y-%m-%d")
    stem = f"digest-{date_label}"

    md = render_markdown(digest)
    html_full = render_html(digest)
    (outdir / f"{stem}.json").write_text(
        json.dumps(digest.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (outdir / f"{stem}.md").write_text(md, encoding="utf-8")
    (outdir / f"{stem}.html").write_text(html_full, encoding="utf-8")

    print(
        f"\n📊 汇总：{len(digest.ok_sources)}/{len(digest.results)} 个源成功，共 {digest.total_items} 条"
    )
    for r in digest.failed_sources:
        print(f"   ❌ {r.spec.name}: {r.error[:120]}")
    title = args.title or (
        f"财经摘要 {date_label}｜{digest.total_items} 条"
        f"｜{len(digest.ok_sources)}/{len(digest.results)} 源"
    )

    # 微信渠道正文限长：按整个源区块取舍，产出始终闭合良好
    content, truncated, dropped, per_source = clamp_html(digest, args.max_bytes)
    pushed_path = outdir / f"{stem}.pushed.html"
    pushed_path.write_text(content, encoding="utf-8")
    (outdir / "preview.html").write_text(
        render_preview_page(digest, title, content), encoding="utf-8"
    )

    print(f"💾 已落盘：{outdir / (stem + '.json')}")
    print(f"           {outdir / (stem + '.md')}")
    print(f"           {outdir / (stem + '.html')}          完整版（{_blen(html_full)} 字节）")
    print(f"           {outdir / (stem + '.pushed.html')}   微信实际会收到的正文（{_blen(content)} 字节）")
    print(f"           {outdir / 'preview.html'}                 手机外壳预览，浏览器打开即可核对")
    if truncated:
        bits: List[str] = []
        if per_source:
            bits.append(f"每源仅保留前 {per_source} 条")
        if dropped:
            shown = "、".join(dropped[:6]) + ("…" if len(dropped) > 6 else "")
            bits.append(f"整源省略 {len(dropped)} 个：{shown}")
        print(f"✂️  正文超过 --max-bytes={args.max_bytes}：{'；'.join(bits)}")
        print(f"   想全量推送就调大上限，例如 --max-bytes {_blen(html_full) + 2000}")

    # ---------- 6.3 推送 ---------- #
    if not do_push:
        print("\n🏁 --dry-run：已跳过微信推送。确认 output/ 里的摘要没问题后，去掉 --dry-run 再跑一次即可。")
        return 0

    if not push_token:
        print(
            "\n❌ 缺少 PUSHPLUS_TOKEN，无法推送。\n"
            "   到 https://www.pushplus.plus/ 扫码登录 → 首页复制『用户 token』→ 填进 .env",
            file=sys.stderr,
        )
        return 3

    print(f"\n📲 推送到 PushPlus（channel={args.channel}, template={args.template}）…")
    print(f"   标题：{title}")
    resp = push_pushplus(
        push_token,
        title,
        content,
        template=args.template,
        channel=args.channel,
        topic=args.topic,
        to=args.to,
    )

    if resp.get("code") == 200:
        print(f"✅ 推送成功：{resp.get('msg', 'OK')}  data={resp.get('data')}")
        return 0

    print(f"❌ 推送失败：{resp}", file=sys.stderr)
    print(
        "   常见原因：token 无效/未关注『pushplus推送加』公众号、免费额度用尽、"
        "channel 未配置、content 超长。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
