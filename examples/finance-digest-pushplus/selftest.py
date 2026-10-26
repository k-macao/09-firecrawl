#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finance_digest.py 的自检脚本（零依赖，直接 `python selftest.py` 跑）。

覆盖那些"跑一次真实抓取要花额度、错了才知道"的逻辑：
  1. sources.json 能解析，字段合法，--only 过滤生效
  2. 中英文源各自命中正确的 extract prompt
  3. Digest JSON 往返不丢数据
  4. clamp_html 在任何预算下都产出闭合良好、且不超预算的 HTML
  5. PushPlus 请求体字段名与官方接口一致，成功/失败判定正确
"""

from __future__ import annotations

import html.parser
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finance_digest import (  # noqa: E402
    Digest,
    NewsItem,
    SourceResult,
    SourceSpec,
    _blen,
    _md_text,
    _md_url,
    clamp_html,
    load_sources,
    pick_prompt,
    push_pushplus,
    render_html,
    render_markdown,
    SOURCES_PATH,
)

PASS = 0
FAIL: List[str] = []

def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL.append(f"{name} {detail}".strip())
        print(f"  ❌ {name} {detail}")

class _StrictParser(html.parser.HTMLParser):
    """记录标签配对情况，用来验证产出的 HTML 没有半个标签。"""

    VOID = {"br", "img", "hr", "meta", "link", "input"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if not self.stack:
            self.errors.append(f"多余的 </{tag}>")
        elif self.stack[-1] != tag:
            self.errors.append(f"标签错位：期待 </{self.stack[-1]}> 实际 </{tag}>")
        else:
            self.stack.pop()

def _sample_digest() -> Digest:
    """构造一个覆盖各种边界形态的假 digest：有指标、无链接、失败源、空条目带 markdown。"""
    results = [
        SourceResult(
            spec=SourceSpec(id="a", name="源A", url="https://a.example", category="news",
                            category_name="新闻", scenario="测试场景"),
            ok=True,
            items=[
                NewsItem(title="带指标的条目", url="https://a.example/1", published_at="5分钟前",
                         summary="摘要<script>alert(1)</script>", company_name="某公司",
                         stock_code="SH600000",
                         key_metrics={"revenue": "100亿元", "yoy": "+15%"}),
                NewsItem(title="无链接无时间的条目", summary="只有摘要"),
            ],
            markdown="# 原始 markdown",
            status_code=200,
            proxy_used="stealth",
            fetched_url="https://a.example/redirected",
        ),
        SourceResult(
            spec=SourceSpec(id="b", name="源B", url="https://b.example", category="news",
                            category_name="新闻", note="付费墙提示"),
            ok=False, error="HTTP 403: 被反爬拦截",
        ),
        SourceResult(
            spec=SourceSpec(id="c", name="源C", url="https://c.example", category="filings",
                            category_name="公告"),
            ok=True, items=[], markdown="|" + " 很长的表格 " * 200 + "|",
        ),
        SourceResult(
            spec=SourceSpec(id="d", name="源D", url="https://d.example", category="community",
                            category_name="社区"),
            ok=True,
            items=[NewsItem(title=f"社区条目 {i}", url=f"https://d.example/{i}",
                            summary="很长的摘要" * 40) for i in range(30)],
        ),
    ]
    return Digest(generated_at="2026-10-26 15:32:07 CST", date_label="2026-10-26", results=results)

# --------------------------------------------------------------------------- #
print("\n[1] sources.json 解析")
specs, prompts = load_sources(SOURCES_PATH)
ids = [s.id for s in specs]
check("enabled 源被加载", len(specs) >= 8, f"实际 {len(specs)}")
check("付费墙源默认启用（用户要求一起试）",
      {"caixin", "bloomberg-markets", "mbhj"} <= set(ids), str(ids))
all_specs, _ = load_sources(SOURCES_PATH, include_disabled=True)
check("include_disabled 不会重复加载已启用的源", len(all_specs) == len(specs),
      f"{len(all_specs)} vs {len(specs)}")
check("三大分类齐全",
      {s.category for s in specs} == {"news", "filings", "community"}, str({s.category for s in specs}))
check("源 id 唯一", len(set(ids)) == len(ids))
check("每个源都有 url/name/id", all(s.url and s.name and s.id for s in specs))
check("中英文 prompt 都非空且含 {limit} 占位",
      bool(prompts["zh"]) and bool(prompts["en"])
      and "{limit}" in prompts["zh"] and "{limit}" in prompts["en"])
check("actions 结构合法",
      all(isinstance(a, dict) and "type" in a for s in specs for a in (s.actions or [])))

print("\n[2] prompt 语言路由")
by_id = {s.id: s for s in all_specs}
check("华尔街见闻 → 中文 prompt", pick_prompt(prompts, by_id["wallstreetcn-live"]) == prompts["zh"].replace("{limit}", str(by_id["wallstreetcn-live"].limit)))
check("Reuters → 英文 prompt", pick_prompt(prompts, by_id["reuters-markets"]).startswith("This is a financial"))
check("SEC EDGAR → 英文 prompt", pick_prompt(prompts, by_id["sec-edgar-10q"]).startswith("This is a financial"))
check("Seeking Alpha → 英文 prompt", pick_prompt(prompts, by_id["seekingalpha-market-news"]).startswith("This is a financial"))
check("雪球 → 中文 prompt", pick_prompt(prompts, by_id["xueqiu-today"]).startswith("这是一个财经资讯"))
check("{limit} 已被替换成真实数字", "{limit}" not in pick_prompt(prompts, by_id["yicai-news"]))
# caixin.com 实测 302 到英文站 caixinglobal.com，光按域名判断会用错语言的 prompt
check("财新 language=en 覆盖了域名判断",
      by_id["caixin"].language == "en"
      and pick_prompt(prompts, by_id["caixin"]).startswith("This is a financial"))
check("Bloomberg → 英文 prompt",
      pick_prompt(prompts, by_id["bloomberg-markets"]).startswith("This is a financial"))
check("迈博汇金 → 中文 prompt",
      pick_prompt(prompts, by_id["mbhj"]).startswith("这是一个财经资讯"))
_fallback = SourceSpec(id="x", name="x", url="https://caixinglobal.com/", category="news",
                       category_name="news")
check("caixinglobal.com 域名本身也走英文", pick_prompt(prompts, _fallback).startswith("This is a financial"))

print("\n[3] Digest JSON 往返")
d = _sample_digest()
payload = d.to_json()
rt = Digest.from_json(json.loads(json.dumps(payload, ensure_ascii=False)))
check("源数量不变", len(rt.results) == len(d.results))
check("条目数量不变", rt.total_items == d.total_items, f"{rt.total_items} vs {d.total_items}")
check("成功/失败判定不变",
      [r.ok for r in rt.results] == [r.ok for r in d.results])
check("key_metrics 原样保留",
      rt.results[0].items[0].key_metrics == {"revenue": "100亿元", "yoy": "+15%"})
check("fetched_url / note / status_code / proxy_used 保留",
      rt.results[0].fetched_url == "https://a.example/redirected"
      and rt.results[1].note == ""
      and rt.results[0].status_code == 200
      and rt.results[0].proxy_used == "stealth")
check("失败源的 error 保留", rt.results[1].error.startswith("HTTP 403"))
check("collector 保留", rt.collector == d.collector)

print("\n[4] HTML 渲染与限长")
full = render_html(d)
p = _StrictParser(); p.feed(full)
check("完整版 HTML 标签闭合", not p.errors and not p.stack, f"{p.errors[:3]} 未闭合={p.stack}")
check("XSS 被转义", "<script>alert(1)</script>" not in full and "&lt;script&gt;" in full)
check("重定向地址不串进正文", "a.example/redirected" not in full)
md = render_markdown(d)
check("Markdown 含分类与失败源", "## 新闻" in md and "❌ 源B" in md and "HTTP 403" in md)
check("Markdown 转义了 script 原文", "<script>alert(1)</script>" in md)  # md 不执行，保留原文更利于排查

print("\n[4b] Markdown 卫生（hallmark / remark-lint 约束）")
# 这些断言锁住 render_markdown 的排版契约。仓库用 hallmark（remark-lint 风格指南）
# 做 markdown CI，产物必须天生干净，而不是靠 CI 每次重写一遍。


def _prose(text: str) -> str:
    """去掉围栏代码块与行内代码，只留会被 remark 当排版解析的散文。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return "".join(re.split(r"`[^`]*`", text))


def _strip_emphasis(text: str) -> str:
    """去掉成对的 _强调_。

    hallmark 约定 emphasis 用 `_`，所以这些下划线是排版记号、是想要的；
    要抓的是 q3_revenue / wait_for 这类标识符里散落的下划线。
    """
    return re.sub(r"(?<!\\)_[^_\n]+_(?!\\)", "", text)


check("_md_url 给 http(s) 套自动链接尖括号", _md_url("https://a.example/x") == "<https://a.example/x>")
check("_md_url 对非 URL 退回行内代码", _md_url("api.example/live") == "`api.example/live`")
check("_md_url 空值安全", _md_url("") == "" and _md_url(None) == "")
check("_md_text 转义散文里的下划线", _md_text("wait_for=4000") == "wait\\_for=4000", _md_text("wait_for=4000"))
check("_md_text 转义方括号（否则被当成引用链接）", _md_text("[html]/[text]") == "\\[html\\]/\\[text\\]", _md_text("[html]/[text]"))
check("_md_text 不动行内代码里的标识符",
      _md_text("`q3_revenue` 与 q3_revenue") == "`q3_revenue` 与 q3\\_revenue",
      _md_text("`q3_revenue` 与 q3_revenue"))

prose = _strip_emphasis(_prose(md))
check("渲染结果里没有裸 URL（remark-lint:no-literal-urls）",
      all(md[m.start() - 1:m.start()] == "<" for m in re.finditer(r"https?://", md)),
      str([md[m.start() - 12:m.start() + 20] for m in re.finditer(r"https?://", md)
           if md[m.start() - 1:m.start()] != "<"][:2]))
check("散文里没有未转义的下划线（no-undefined-references / emphasis-marker）",
      not re.search(r"(?<!\\)_", prose),
      str(re.findall(r".{0,18}(?<!\\)_.{0,18}", prose)[:3]))
check("散文里没有未转义的方括号",
      not re.search(r"(?<!\\)[\\[\\]]", prose),
      str(re.findall(r".{0,18}(?<!\\)[\\[\\]].{0,18}", prose)[:3]))
check("以且仅以一个换行结尾（remark-lint:final-newline）",
      md.endswith("\n") and not md.endswith("\n\n"))

_md_lines = md.splitlines()
_bad_heading = [l for i, l in enumerate(_md_lines)
                if l.startswith("###") and i + 1 < len(_md_lines) and _md_lines[i + 1].strip() != ""]
check("每个 ### 标题后紧跟空行（否则 remark 会并掉相邻列表）",
      not _bad_heading, str(_bad_heading[:2]))

_i1 = next(i for i, l in enumerate(_md_lines) if l.startswith("1. "))
_i10 = next(i for i, l in enumerate(_md_lines) if l.startswith("10. "))
check("一位数序号下的嵌套项缩进 3 空格",
      _md_lines[_i1 + 1].startswith("   - ") and not _md_lines[_i1 + 1].startswith("    - "),
      repr(_md_lines[_i1 + 1][:8]))
# `10. ` 的内容列在第 5 列；固定 3 空格会让子项被判成同级新列表，整片报 list-item-bullet-indent
check("两位数序号下的嵌套项缩进 4 空格（跟随标记宽度）",
      _md_lines[_i10 + 1].startswith("    - ") and not _md_lines[_i10 + 1].startswith("     - "),
      repr(_md_lines[_i10 + 1][:8]))
check("场景说明用 _ 强调而非 *（hallmark emphasis 约定）",
      "_测试场景_" in md and "*测试场景*" not in md)
check("页脚用 _ 强调而非 *",
      "_由 Firecrawl 抓取并结构化提取，经 PushPlus 推送到微信。_" in md)

for cap in (200000, 40000, 24000, 12000, 6000, 2500, 1200):
    out, truncated, dropped, per_source = clamp_html(d, cap)
    pp = _StrictParser(); pp.feed(out)
    closed = not pp.errors and not pp.stack
    fits = _blen(out) <= cap or (not dropped and per_source in (None, 1))  # 预算小到连头尾都放不下时兜底
    check(f"cap={cap:>6} 字节={_blen(out):>6} 闭合={closed} 在预算内={fits}",
          closed and fits, f"errors={pp.errors[:2]} stack={pp.stack}")
    check(f"cap={cap:>6} 分类标题永不被整源丢弃", all(not x.startswith("[分类]") for x in dropped))
    if per_source:
        # 不变式：只要输出里的条目总数超过"每源上限"，就必然有源被裁过，标题就该标注出来。
        # （极端小预算下所有源区块都被丢掉时，条目数为 0，同样满足。）
        li_count = out.count("<li>")
        check(f"cap={cap:>6} 裁条数后标题标注了实际条数",
              "仅显示前" in out or li_count <= per_source,
              f"li={li_count} > per_source={per_source} 却无标注")

# 第一级降载应优先"减条数"而不是"丢整源"：预算够放全部源、只是放不下全部条目时，dropped 必须为空
mid = _blen(render_html(d))
out, tr, dropped, per_source = clamp_html(d, int(mid * 0.75))
check("中等预算下只减条数、不丢源", tr and dropped == [] and per_source is not None,
      f"dropped={dropped} per_source={per_source}")
check("中等预算下四个源标题都还在",
      all(n in out for n in ("源A", "源B", "源C", "源D")))

# 预算极小时才允许整源丢弃
out, tr, dropped, per_source = clamp_html(d, 2500)
check("极小预算下启用整源丢弃", tr and len(dropped) > 0, f"dropped={dropped}")

_, tr_small, _, ps_small = clamp_html(d, 200000)
check("预算充足时不截断", tr_small is False and ps_small is None)

print("\n[5] PushPlus 请求体")

captured: Dict[str, Any] = {}

class _FakeResp:
    status_code = 200

    def __init__(self, body: Dict[str, Any]) -> None:
        self._body = body

    def json(self) -> Dict[str, Any]:
        return self._body

class _FakeRequests:
    """顶替 requests 模块；push_pushplus 内部是延迟 import，注入 sys.modules 即可。"""

    @staticmethod
    def post(url: str, json: Dict[str, Any] = None, timeout: int = None) -> _FakeResp:  # noqa: A002
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        return _FakeResp({"code": 200, "msg": "请求成功", "data": "shortcode123"})

_saved = sys.modules.get("requests")
sys.modules["requests"] = _FakeRequests  # type: ignore[assignment]
try:
    resp = push_pushplus("TOKEN123", "财经摘要 2026-10-26", "<div>hi</div>",
                         template="html", channel="wechat", topic="finance")
finally:
    if _saved is None:
        sys.modules.pop("requests", None)
    else:
        sys.modules["requests"] = _saved

pay = captured.get("payload", {})
check("打到官方 send 接口", captured.get("url") == "https://www.pushplus.plus/send", str(captured.get("url")))
check("token 正确", pay.get("token") == "TOKEN123")
check("title 正确", pay.get("title") == "财经摘要 2026-10-26")
check("content 正确", pay.get("content") == "<div>hi</div>")
check("template=html", pay.get("template") == "html")
check("channel=wechat", pay.get("channel") == "wechat")
check("topic 透传", pay.get("topic") == "finance")
check("未设置 to 时不下发空字段", "to" not in pay and "option" not in pay)
check("code==200 判定为成功", resp.get("code") == 200)
check("以 JSON body 发送（Content-Type: application/json）", isinstance(pay, dict))

print("\n" + "=" * 62)
if FAIL:
    print(f"❌ {len(FAIL)} 项未通过：")
    for f in FAIL:
        print(f"   - {f}")
    sys.exit(1)
print(f"✅ 全部 {PASS} 项通过")
