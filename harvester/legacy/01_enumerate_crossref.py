# -*- coding: utf-8 -*-
"""Crossref 整刊清单枚举：ISSN + 年份段 -> manifest.csv。

零验证码、零 token、纯 API。产出 manifest 是后续所有阶段的唯一账本。
用法见同目录 SKILL.md 第 1 段。
"""
import argparse
import csv
import os
import sys
import time

import requests

try:
    sys.stdout.reconfigure(errors="replace")  # 跟随本机控制台编码，GBK 下不乱码、异常字符不崩
except Exception:
    pass

API = "https://api.crossref.org/journals/{issn}/works"
FIELDS = ["source", "doi", "pii", "title", "year", "month", "first_author",
          "journal", "volume", "issue", "pages", "type", "url",
          "oa_channel", "status", "file", "note"]
SELECT = ("DOI,title,container-title,issued,author,volume,issue,page,type,"
          "ISSN,alternative-id,URL")


def get_mailto():
    """Crossref polite pool 要求带联系邮箱，复用 UNPAYWALL_EMAIL（含 Windows 用户级回退）。"""
    v = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    if not v and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, "UNPAYWALL_EMAIL")[0]).strip()
        except Exception:
            v = ""
    return v


def fetch_page(sess, issn, flt, cursor, mailto, rows, use_select=True, tries=4):
    params = {"filter": flt, "rows": rows, "cursor": cursor, "mailto": mailto}
    if use_select:
        params["select"] = SELECT
    for i in range(tries):
        try:
            r = sess.get(API.format(issn=issn), params=params, timeout=90)
            if r.status_code == 200:
                return r.json()["message"]
            if r.status_code == 400 and use_select:
                # select 字段名若不被端点接受，退回全量记录重试
                return fetch_page(sess, issn, flt, cursor, mailto, rows,
                                  use_select=False)
            if r.status_code == 404:
                raise SystemExit(f"[fatal] ISSN {issn} 在 Crossref 不存在"
                                 f"（检查是否该用 eISSN；中文刊多数无收录，走知网通道）")
            if r.status_code in (429, 500, 502, 503):
                wait = 2 ** i * 3
                print(f"[warn] HTTP {r.status_code}，退避 {wait}s", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[warn] network: {e}", flush=True)
            time.sleep(2 ** i)
    raise RuntimeError(f"Crossref 翻页失败：issn={issn} cursor={cursor[:20]}")


def to_row(item, issn):
    title = (item.get("title") or [""])[0].strip()
    cont = (item.get("container-title") or [""])[0].strip()
    # date-parts 可能只有年份 [[2024]] 甚至 [[]]，缺月份不能崩（JAS/JDS 实测踩中）
    dp = (item.get("issued") or {}).get("date-parts") or []
    first = dp[0] if dp and isinstance(dp[0], list) else []
    year = first[0] if len(first) > 0 else ""
    month = first[1] if len(first) > 1 else ""
    auth = item.get("author") or []
    fam = (auth[0].get("family") or auth[0].get("name") or "").strip() if auth else ""
    # Elsevier 的 PII 在 alternative-id；其他出版商该字段可能有别的值，仅 10.1016 用到
    alt = item.get("alternative-id") or []
    pii = ""
    if alt:
        cand = alt[0]
        pii = cand.get("alternative-id", "") if isinstance(cand, dict) else str(cand)
    return {
        "source": f"crossref:{issn}", "doi": (item.get("DOI") or "").lower(),
        "pii": pii, "title": title, "year": year or "", "month": month or "",
        "first_author": fam, "journal": cont,
        "volume": item.get("volume", ""), "issue": item.get("issue", ""),
        "pages": item.get("page", ""), "type": item.get("type", ""),
        "url": item.get("URL", ""), "oa_channel": "", "status": "pending",
        "file": "", "note": "",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--issn", action="append", required=True,
                    help="可重复：print 与 eISSN 各传一次")
    ap.add_argument("--from", dest="date_from", default=None, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", default=None, help="YYYY-MM-DD")
    ap.add_argument("--types", default="journal-article",
                    help='默认 journal-article；传空字符串不过滤')
    ap.add_argument("--rows", type=int, default=1000)
    ap.add_argument("--out", required=True, help="manifest.csv 输出路径")
    ap.add_argument("--env-proxy", action="store_true",
                    help="走系统代理环境变量（默认绕过，继承 harvester 教训）")
    args = ap.parse_args()

    sess = requests.Session()
    sess.trust_env = args.env_proxy
    sess.headers.update({"User-Agent": "journal-bulk-harvester/1.0"})
    mailto = get_mailto()
    if not mailto:
        print("[warn] 未设置 UNPAYWALL_EMAIL：Crossref polite pool 与 Unpaywall 通道都会变弱，"
              "建议 setx UNPAYWALL_EMAIL 你的真实邮箱", flush=True)

    flt_parts = []
    if args.date_from:
        flt_parts.append(f"from-pub-date:{args.date_from}")
    if args.date_to:
        flt_parts.append(f"until-pub-date:{args.date_to}")
    if args.types:
        flt_parts.append(f"type:{args.types}")
    flt = ",".join(flt_parts)

    by_doi = {}
    for issn in args.issn:
        cursor, page = "*", 1
        while True:
            msg = fetch_page(sess, issn, flt, cursor, mailto, args.rows)
            items = msg.get("items") or []
            if not items:
                break
            for it in items:
                row = to_row(it, issn)
                if row["doi"] and row["doi"] not in by_doi:
                    by_doi[row["doi"]] = row
            print(f"[{issn}] page {page}: +{len(items)}  累计唯一 {len(by_doi)}",
                  flush=True)
            cursor = msg.get("next-cursor")
            page += 1
            time.sleep(1.2)
            if not cursor:
                break

    rows = sorted(by_doi.values(),
                  key=lambda r: (str(r["year"]).zfill(5), r["first_author"], r["title"]))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    years = {}
    for r in rows:
        years[r["year"]] = years.get(r["year"], 0) + 1
    print(f"\n== 完成：{len(rows)} 篇 -> {args.out}")
    print("   年份分布:", dict(sorted(years.items())))
    print("   下一步：先看规模，与用户确认全下/筛之后再跑 02_download_pdfs.py")


if __name__ == "__main__":
    main()
