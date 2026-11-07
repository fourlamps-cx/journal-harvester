# -*- coding: utf-8 -*-
r"""万方期刊页只读题录抓取（遗传/发育相关栏目）——中文刊枚举模拟。

边界：只读浏览列表页（goto+DOM 文本），不点击任何下载按钮，每页间隔 4-6s。
产出：与 journal-bulk-harvester manifest 同构的 CSV，供 cnki_manifest.py
后续 match / 03 汇总复用。PDF 下载不在本脚本范围（走知网手动批量或 CDP 层）。

用法：python wanfang_crawl.py --out-dir <manifests目录>
"""
import argparse
import csv
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

FIELDS = ["source", "doi", "pii", "title", "year", "month", "first_author",
          "journal", "volume", "issue", "pages", "type", "url",
          "oa_channel", "status", "file", "note"]

JOURNALS = [
    ("中国畜牧杂志", "zgxmzz", ["遗传育种"]),
    ("畜牧兽医学报", "xmsyxb", ["遗传育种", "遗传学"]),
    ("遗传", "yc", ["遗传学", "综述"]),
]
# 栏目名含这些词才算"遗传/发育"相关
COL_KW = re.compile(r"遗传|育种|发育|繁殖生理|胚胎")


def body_text(page):
    return page.evaluate("document.body.innerText")


def wait_ready(page, min_links=25, tries=8):
    for _ in range(tries):
        time.sleep(4)
        try:
            n = page.eval_on_selector_all("a", "els => els.length")
        except Exception:
            n = 0
        if n >= min_links and "文献量" in body_text(page):
            return True
    return False


COL_TAGS = {"综述", "研究简报", "学术研究", "遗传育种", "本期内检索",
            "本期封面/目录/封底页", "动物营养与饲料科学", "专题综述"}


def harvest_items(page, journal, year_hint):
    """全文行扫描解析。条目边界 = 页码行(1-6) 或 期次行(2026年10期)。
    结构：[栏目?] 题名 作者 页码/期次。"""
    txt = body_text(page)
    lines = [l.strip() for l in txt.splitlines() if l.strip()]

    def is_boundary(l):
        return re.fullmatch(r"\d+(-\d+|\+\d+)?", l) or \
            re.fullmatch(r"(19|20)\d{2}年\d+期", l)

    got, buf = [], []
    for ln in lines:
        if is_boundary(ln) and buf:
            core = [b for b in buf if b not in COL_TAGS and
                    not re.fullmatch(r"(19|20)\d{2}年\d+期", b)]
            if len(core) >= 2:
                title, authors = core[-2], core[-1]
                # 修正：末行不是作者（无逗号且更像机构/期次）时回退一行
                if "," not in authors and "，" not in authors and len(core) > 2:
                    title, authors = core[-3], core[-2]
                first = re.split(r"[,，]", authors)[0].strip()
                if 6 <= len(title) <= 120 and not first.endswith("："):
                    got.append({"title": title, "first_author": first,
                                "pages": ln if re.fullmatch(r"\d+(-\d+)?", ln) else ""})
            buf = []
        else:
            buf.append(ln)
            if len(buf) > 4:
                buf = buf[-4:]
    out = []
    for it in got:
        r = {k: "" for k in FIELDS}
        r.update(source="wanfang", doi="", pii="", title=it["title"],
                 year=year_hint, first_author=it["first_author"],
                 journal=journal, pages=it["pages"], url="",
                 type="journal-article", status="pending",
                 note="wanfang-readonly-crawl")
        out.append(r)
    return out


def discover_columns(page):
    """抓当前期刊页的栏目 tab 文本（ztext 参数值）。"""
    tabs = page.eval_on_selector_all(
        "a[href*='tabId=column']",
        "els => els.map(e => decodeURIComponent(e.href.split('ztext=')[1] || ''))")
    return sorted({t for t in tabs if t})


def crawl_journal(page, name, wid, out_dir, max_pages=2, col_cands=None):
    base = f"https://c.wanfangdata.com.cn/magazine/{wid}/"
    page.goto(base, timeout=60000, wait_until="domcontentloaded")
    if not wait_ready(page):
        print(f"[{name}] 页面未就绪（id 可能不对）", flush=True)
        return []
    cols = discover_columns(page) or []
    picked = [c for c in (cols or []) if COL_KW.search(c)]
    if not picked:
        picked = [c for c in (col_cands or []) if c]   # 已验证可用的栏目名
    print(f"[{name}] 栏目发现 {len(cols)}，采用: {picked or '(无->全部栏目+题名过滤)'}",
          flush=True)

    rows, seen = [], set()
    targets = [(f"?tabId=column&ztext={c}", c) for c in picked]
    for suffix, col in targets:
        for pg in range(1, max_pages + 1):
            url = base + suffix + f"&page={pg}&yearType=1"
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            if not wait_ready(page):
                break
            got = harvest_items(page, name, "2025-2026")
            new = [g for g in got if g["title"] not in seen]
            for g in new:
                seen.add(g["title"])
                g["note"] = f"wanfang-readonly-crawl col={col}"
            rows.extend(new)
            print(f"[{name}] {col} p{pg}: +{len(new)} (累计 {len(rows)})", flush=True)
            if not new:
                break
            time.sleep(4.5)
    if not rows:   # 栏目名不对 -> 全部栏目 + 题名主题过滤
        page.goto(base + "?tabId=column&page=1", timeout=60000,
                  wait_until="domcontentloaded")
        if wait_ready(page):
            got = harvest_items(page, name, "2025-2026")
            got = [g for g in got if TOPIC_KW.search(g["title"])]
            for g in got:
                g["note"] = "wanfang-readonly-crawl col=全部栏目+题名过滤"
            rows.extend(got)
            print(f"[{name}] 全部栏目+题名过滤: +{len(got)}", flush=True)
    out = os.path.join(out_dir, f"wanfang_{wid}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"[{name}] == 完成 {len(rows)} 条 -> {out}", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-pages", type=int, default=2)
    ap.add_argument("--journal", action="append", default=[],
                    help="覆盖默认刊表，格式 \"名称:万方id[:候选栏目1|栏目2]\"，可重复")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    journals = JOURNALS
    if args.journal:
        journals = []
        for spec in args.journal:
            parts = spec.split(":")
            name, wid = parts[0], parts[1]
            cands = parts[2].split("|") if len(parts) > 2 else []
            journals.append((name, wid, cands))
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        total = 0
        for name, wid, col_cands in journals:
            try:
                total += len(crawl_journal(page, name, wid, args.out_dir,
                                           args.max_pages, col_cands))
            except Exception as e:
                print(f"[{name}] EXC {type(e).__name__}: {str(e)[:120]}", flush=True)
            time.sleep(5)
        page.close()
        print(f"\n== 中文刊模拟枚举完成：共 {total} 条题录（只读，未触碰下载）")


if __name__ == "__main__":
    main()
