# -*- coding: utf-8 -*-
"""第 3 段：复验 magic bytes、出汇总表。agent 只读汇总 md，不翻 manifest 原始行。"""
import argparse
import csv
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(errors="replace")  # 跟随本机控制台编码，GBK 下不乱码、异常字符不崩
except Exception:
    pass

FIELDS = ["source", "doi", "pii", "title", "year", "month", "first_author",
          "journal", "volume", "issue", "pages", "type", "url",
          "oa_channel", "status", "file", "note"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out-dir", required=True, help="PDF 所在目录")
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        rows = list(rdr)

    downgraded = 0
    for r in rows:
        if r["status"] != "ok":
            continue
        p = os.path.join(args.out_dir, r["file"])
        if not os.path.exists(p):
            r["status"], r["note"] = "failed", (r["note"] + " verify:file-missing").strip()
            downgraded += 1
            continue
        with open(p, "rb") as f:
            head = f.read(2048)
        size = os.path.getsize(p)
        if not (head[:5] == b"%PDF-" or (size > 20000 and b"%PDF-" in head)) or size < 20480:
            r["status"], r["note"] = "failed", (r["note"] + " verify:not-pdf").strip()
            downgraded += 1
            os.rename(p, p + ".badhtml")
    if downgraded:
        with open(args.manifest, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        print(f"[verify] {downgraded} 个文件复验不过，已降级为 failed（坏文件改后缀 .badhtml）")
    else:
        print("[verify] 所有 ok 文件复验通过")

    st = Counter(r["status"] for r in rows)
    ch = Counter(r["oa_channel"] for r in rows if r["status"] == "ok")
    yr = Counter(r["year"] for r in rows)

    stamp = __import__("time").strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(os.path.dirname(args.manifest), f"汇总_{stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    md_path = csv_path[:-4] + ".md"
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(f"# 整刊下载汇总（{stamp}）\n\n")
        fh.write(f"- 总量：{len(rows)} ｜ ok：{st.get('ok', 0)} ｜ "
                 f"paywalled：{st.get('paywalled', 0)} ｜ failed：{st.get('failed', 0)} ｜ "
                 f"pending：{st.get('pending', 0)}\n\n")
        fh.write("## 成功通道分布\n\n| 通道 | 篇数 |\n|---|---|\n")
        for k, v in ch.most_common():
            fh.write(f"| {k or '(空)'} | {v} |\n")
        fh.write("\n## 年份分布（全部条目）\n\n| 年份 | 篇数 |\n|---|---|\n")
        for k in sorted(yr):
            fh.write(f"| {k} | {yr[k]} |\n")
        fh.write("\n## 付费墙清单（待人工/下一轮会话）\n\n| 年份 | 题名 | 落地页 |\n|---|---|---|\n")
        for r in rows:
            if r["status"] == "paywalled":
                fh.write(f"| {r['year']} | {r['title'][:80]} | https://doi.org/{r['doi']} |\n")
        fh.write("\n## 失败清单（含原因）\n\n| DOI | 原因 |\n|---|---|\n")
        for r in rows:
            if r["status"] == "failed":
                fh.write(f"| {r['doi']} | {r['note'] or '(无记录)'} |\n")

    print(f"== 汇总：{md_path}\n   全量：{csv_path}")
    print("agent/用户只需读汇总 md 的付费墙与失败两节。")


if __name__ == "__main__":
    main()
