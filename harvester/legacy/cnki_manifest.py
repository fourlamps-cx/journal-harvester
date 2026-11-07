# -*- coding: utf-8 -*-
"""中文核心（知网/万方）通道：题录导出 -> manifest；页面批量下载的 PDF 回填匹配。

两个子命令：
  parse  知网 EndNote 格式导出（%T/%A/%D/...）或自定义/CSV 导出 -> manifest.csv
  match  把「页面勾选批量下载」得到的 PDF 目录按题名模糊匹配回填 manifest
         （Jaccard >= 0.55 才接受，宁缺毋滥；低于阈值列入人工清单）

注意：不写脚本去自动点知网页面。知网风控敏感，量大批量走页面自带的
「勾选 -> 批量下载」，每批别超过 ~50 篇。
"""
import argparse
import csv
import os
import re
import sys
import unicodedata
from collections import defaultdict

try:
    sys.stdout.reconfigure(errors="replace")  # 跟随本机控制台编码，GBK 下不乱码、异常字符不崩
except Exception:
    pass

FIELDS = ["source", "doi", "pii", "title", "year", "month", "first_author",
          "journal", "volume", "issue", "pages", "type", "url",
          "oa_channel", "status", "file", "note"]


def blank_row(source):
    r = {k: "" for k in FIELDS}
    r.update(source=source, type="journal-article", status="pending")
    return r


# ---------- parse ----------

ENW_MAP = {"T": "title", "J": "journal", "D": "year", "V": "volume",
           "N": "issue", "P": "pages", "R": "doi", "@": "pages"}

CSV_HDR_MAP = {
    "title": ["题名", "标题", "文献标题", "篇名", "title"],
    "first_author": ["作者", "作者姓名", "authors", "author", "第一作者"],
    "year": ["年份", "发表年份", "出版年", "年", "year", "pubyear"],
    "journal": ["期刊", "来源", "刊名", "source", "journal"],
    "doi": ["doi", "doi号"],
    "volume": ["卷", "volume"], "issue": ["期", "issue"],
    "pages": ["页", "页码", "page", "pages"],
    "url": ["url", "链接", "原文链接", "下载链接"],
}


def parse_enw(text):
    rows, cur = [], None
    for line in text.splitlines():
        line = line.rstrip()
        m = re.match(r"^%(\w)\s+(.*)$", line)
        if not m:
            if line.upper().startswith("ER") and cur:
                rows.append(cur)
                cur = None
            continue
        tag, val = m.group(1).upper(), m.group(2).strip()
        if tag in ("0", "X") and cur and cur.get("title"):
            rows.append(cur)
            cur = None
        if tag == "0" and cur is None:
            cur = blank_row("cnki")
        if cur is None:
            cur = blank_row("cnki")
        key = ENW_MAP.get(tag)
        if not key:
            continue
        if key == "first_author":
            if not cur["first_author"]:
                cur["first_author"] = val.split(";")[0].split(",")[0].strip()
        elif key == "title" and not cur["title"]:
            cur["title"] = val
        elif key == "pages":
            cur["pages"] = cur["pages"] or val
        elif key == "year":
            ym = re.search(r"(19|20)\d{2}", val)
            cur["year"] = ym.group(0) if ym else val
        else:
            cur[key] = cur[key] or val
    if cur and cur.get("title"):
        rows.append(cur)
    return rows


def parse_csv_text(text, fname):
    import io
    delim = "\t" if text.count("\t") > text.count(",") else ","
    rdr = csv.DictReader(io.StringIO(text), delimiter=delim)
    hdr = {(h or "").strip().lstrip("\ufeff").lower(): h for h in (rdr.fieldnames or [])}
    colmap = {}
    for target, cands in CSV_HDR_MAP.items():
        for c in cands:
            if c.lower() in hdr:
                colmap[target] = hdr[c.lower()]
                break
    if "title" not in colmap:
        print(f"[warn] {fname}: 表头未识别出题名列，跳过（表头={list(hdr.values())}）")
        return []
    rows = []
    for rec in rdr:
        r = blank_row("cnki")
        for target, col in colmap.items():
            v = (rec.get(col) or "").strip()
            if target == "first_author" and v:
                v = re.split(r"[;,，]", v)[0].strip()
            r[target] = v
        if r["title"]:
            rows.append(r)
    return rows


def cmd_parse(args):
    rows = []
    for p in args.export:
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
        if re.search(r"^%\w\s", text, re.M):
            got = parse_enw(text)
        else:
            got = parse_csv_text(text, p)
        print(f"{os.path.basename(p)}: +{len(got)}")
        rows.extend(got)
    # 去重：题名+年份
    seen, uniq = set(), []
    for r in rows:
        k = (re.sub(r"\W", "", r["title"].lower()), r["year"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(uniq)
    print(f"== parse 完成：{len(uniq)} 条 -> {args.out}（去重前 {len(rows)}）")
    print("下一步：知网页面分批勾选批量下载 PDF 到一个目录，然后跑 match 子命令")


# ---------- match ----------

def _tokens(title):
    """中英文混合题名分词：先在中英文边界插空格，英文按词，中文按二元组。"""
    s = unicodedata.normalize("NFKC", title or "").lower()
    s = re.sub(r"([^\u4e00-\u9fff])([\u4e00-\u9fff])", r"\1 \2", s)
    s = re.sub(r"([\u4e00-\u9fff])([^\u4e00-\u9fff])", r"\1 \2", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    toks = []
    for w in s.split():
        if re.fullmatch(r"[\u4e00-\u9fff]+", w):
            toks += [w[i:i + 2] for i in range(len(w) - 1)] or [w]
        else:
            toks.append(w)
    return set(toks)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cmd_match(args):
    with open(args.manifest, encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        rows = list(rdr)
    pend = [r for r in rows if r["status"] in ("pending", "paywalled")]
    tok_by_row = {id(r): _tokens(r["title"]) for r in pend}
    yr_by_row = {id(r): r["year"] for r in pend}

    used, matched, manual = set(), 0, []
    for fn in sorted(os.listdir(args.pdf_dir)):
        if not fn.lower().endswith(".pdf"):
            continue
        stem = os.path.splitext(fn)[0]
        toks = _tokens(stem)
        yhint = re.search(r"(19|20)\d{2}", stem)
        best, best_score = None, 0.0
        for r in pend:
            if id(r) in used:
                continue
            sc = jaccard(toks, tok_by_row[id(r)])
            if yhint and yr_by_row[id(r)] and yhint.group(0) != yr_by_row[id(r)]:
                sc *= 0.5
            if sc > best_score:
                best, best_score = r, sc
        if best is not None and best_score >= 0.55:
            best.update(status="ok", oa_channel="cnki-manual",
                        file=os.path.abspath(os.path.join(args.pdf_dir, fn)),
                        note=f"match={best_score:.2f}")
            used.add(id(best))
            matched += 1
        else:
            manual.append((fn, round(best_score, 2)))
    with open(args.manifest, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    print(f"== match 完成：回填 {matched} 篇")
    if manual:
        print(f"以下 {len(manual)} 个 PDF 低于阈值/未配上，需人工对：")
        for fn, sc in manual[:40]:
            print(f"   {sc:>5}  {fn}")
        if len(manual) > 40:
            print(f"   ...还有 {len(manual) - 40} 个")
    print("下一步：python 03_verify_and_report.py --manifest ... --out-dir <pdfs目录>")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("parse")
    p1.add_argument("--export", nargs="+", required=True, help="知网导出的题录 txt/csv")
    p1.add_argument("--out", required=True)
    p1.set_defaults(fn=cmd_parse)
    p2 = sub.add_parser("match")
    p2.add_argument("--manifest", required=True)
    p2.add_argument("--pdf-dir", required=True)
    p2.set_defaults(fn=cmd_match)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
