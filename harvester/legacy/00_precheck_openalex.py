# -*- coding: utf-8 -*-
"""OpenAlex 批量预标记：整刊一次拉 OA 状态，回填 manifest 的 oa_url 列。

为什么存在：逐篇 Unpaywall（~5s/篇）跑 20 万篇要几周；OpenAlex 一页 200 条、
带 mailto 进 polite pool，整刊几分钟。预标记后 02 --premarked-only 只对有
oa_url 的行直下，OA 层成本从 O(全量) 降到 O(OA子集)。

用法：
  python 00_precheck_openalex.py --manifests-dir <dir>      # 51 刊逐个标记
  python 00_precheck_openalex.py --manifest <file.csv>      # 单刊

幂等：已带 oa_url 列的刊直接跳过。429 退避重试，仍失败则跳过该刊（可重跑）。
"""
import argparse
import csv
import os
import re
import sys
import time
import urllib.parse

import requests

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

API = "https://api.openalex.org"
FROM = "2016-01-01"
SECRETS_DIR = os.environ.get("HARVESTER_SECRETS_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "secrets"))
API_KEY = ""   # main() 里从 secrets 静默加载


def load_openalex_key():
    """从 secrets 静默加载 OpenAlex key。铁律：绝不打印/记录 key 的值。"""
    import json as _json
    p_env = os.path.join(SECRETS_DIR, ".env")
    p_txt = os.path.join(SECRETS_DIR, "api_key.txt")
    try:
        txt = open(p_env, encoding="utf-8-sig", errors="replace").read()
        for line in txt.splitlines():
            if "=" in line and "OPENALEX" in line.upper():
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v
    except OSError:
        pass
    try:
        txt = open(p_txt, encoding="utf-8-sig", errors="replace").read()
    except OSError:
        return ""
    try:
        data = _json.loads(txt)

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str) and 10 < len(v) < 200 and \
                            ("openalex" in str(k).lower() or "api" in str(k).lower()):
                        return v
                    r = walk(v)
                    if r:
                        return r
            elif isinstance(o, list):
                for v in o:
                    r = walk(v)
                    if r:
                        return r
            return None
        v = walk(data)
        if v:
            return v
    except ValueError:
        pass
    prev = ""
    for line in txt.splitlines():
        toks = line.strip().split()
        # 格式："openalex <key>" 或 "openalex: <key>"（服务名 + key 同行）
        if len(toks) >= 2 and toks[0].lower().rstrip(":") == "openalex":
            v = toks[1].strip().strip('",')
            if 10 <= len(v) <= 100:
                return v
        prev = line
    return ""


def get_mailto():
    v = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    if not v and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, "UNPAYWALL_EMAIL")[0]).strip()
        except Exception:
            v = ""
    return v


def norm_doi(u):
    d = (u or "").lower().strip()
    return d[len("https://doi.org/"):] if d.startswith("https://doi.org/") else d


def oa_pdf_url(it):
    b = it.get("best_oa_location") or {}
    if b.get("pdf_url"):
        return b["pdf_url"]
    oa = it.get("open_access") or {}
    u = oa.get("oa_url") or ""
    return u if u.lower().endswith(".pdf") else ""


def source_id_for(sess, issn, mailto, api_key):
    """OpenAlex 偶发瞬时 503/网络抖动，sources 查询必须带退避重试（2026-11 实测）。"""
    params = {"filter": f"issn:{issn}", "select": "id,display_name",
              "mailto": mailto}
    if api_key:
        params["api_key"] = api_key
    for attempt in range(4):
        try:
            r = sess.get(f"{API}/sources", params=params, timeout=40)
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 200:
            items = r.json().get("results") or []
            return items[0]["id"].rsplit("/", 1)[-1] if items else None
        time.sleep(5 * (attempt + 1))
    return None


def precheck_journal(sess, mpath, mailto):
    with open(mpath, encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        fnames = list(rdr.fieldnames)
        rows = list(rdr)
    if not rows:
        return None
    if "oa_url" not in fnames:
        fnames.append("oa_url")
    elif all((r.get("oa_url") or "").strip() for r in rows):
        return "skip-done"

    issn = (rows[0].get("source") or "").split(":")[-1]
    if not issn:
        return "no-issn"
    sid = source_id_for(sess, issn, mailto, API_KEY)
    if not sid:
        return "no-source"

    # 全刊 OA 状态翻页（只取本次窗口内 journal-article）
    urlmap = {}
    cursor, page = "*", 1
    while True:
        params = {"filter": (f"primary_location.source.id:{sid},"
                             f"from_publication_date:{FROM},type:article"),
                  # 注意 OpenAlex 的 type 是它自己的分类（journal-article→article），
                  # 用 Crossref 的标签会 400（2026-11 实测，报错正文会提示）
                  "select": "doi,open_access,best_oa_location",
                  "per-page": 200, "cursor": cursor, "mailto": mailto}
        if API_KEY:
            params["api_key"] = API_KEY
        r = None
        for attempt in range(6):
            try:
                r = sess.get(f"{API}/works", params=params, timeout=60)
            except requests.RequestException:
                r = None
            if r is not None and r.status_code == 200:
                break
            wait = 20 * (attempt + 1)
            code = r.status_code if r is not None else "net"
            print(f"    [warn] page{page} HTTP {code} 退避 {wait}s", flush=True)
            time.sleep(wait)
        if r is None or r.status_code != 200:
            return f"openalex-fail(page{page})"
        msg = r.json()
        for it in msg.get("results") or []:
            d = norm_doi(it.get("doi"))
            if d:
                u = oa_pdf_url(it)
                if u:
                    urlmap[d] = u
        cursor = msg.get("meta", {}).get("next_cursor")
        page += 1
        time.sleep(1.2)
        if not cursor:
            break

    n_hit = 0
    for r_ in rows:
        u = urlmap.get((r_.get("doi") or "").lower(), "")
        r_["oa_url"] = u
        n_hit += 1 if u else 0
    with open(mpath, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fnames)
        w.writeheader()
        w.writerows(rows)
    return {"n": len(rows), "n_pdf": n_hit, "pages": page - 1}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--manifests-dir")
    g.add_argument("--manifest")
    args = ap.parse_args()

    mailto = get_mailto()
    if not mailto:
        print("[warn] 无 UNPAYWALL_EMAIL：OpenAlex 礼貌池进不去，额度小易 429")
    global API_KEY
    API_KEY = load_openalex_key()
    print(f"[key] OpenAlex key: {'已加载(长度 %d，值不显示)' % len(API_KEY) if API_KEY else '未找到，走匿名共享额度'}",
          flush=True)
    files = ([args.manifest] if args.manifest else
             sorted(os.path.join(args.manifests_dir, f)
                    for f in os.listdir(args.manifests_dir) if f.endswith(".csv")))
    sess = requests.Session()
    sess.trust_env = False
    sess.headers["User-Agent"] = "journal-bulk-harvester-precheck/1.0"

    summary = []
    for i, p in enumerate(files, 1):
        t0 = time.time()
        try:
            res = precheck_journal(sess, p, mailto)
        except Exception as e:
            res = f"exc:{type(e).__name__}"
        name = os.path.basename(p)
        if isinstance(res, dict):
            line = f"[{i}/{len(files)}] {name}: {res['n_pdf']}/{res['n']} 有OA直链 " \
                   f"({res['pages']}页, {time.time()-t0:.0f}s)"
            summary.append({"manifest": name, "n": res["n"], "n_pdf": res["n_pdf"],
                            "status": "ok"})
        else:
            line = f"[{i}/{len(files)}] {name}: {res}"
            summary.append({"manifest": name, "n": "", "n_pdf": "",
                            "status": str(res)})
        print(line, flush=True)

    out = os.path.join(os.path.dirname(files[0]), "precheck_summary.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["manifest", "n", "n_pdf", "status"])
        w.writeheader()
        w.writerows(summary)
    tot = sum(int(s["n_pdf"]) for s in summary if str(s["n_pdf"]).isdigit())
    print(f"\n== 预标记完成：合计 {tot} 篇有 OA 直链 -> {out}")
    print("下一步：对每本刊跑 02 --premarked-only（全量 OA 轮）")


if __name__ == "__main__":
    main()
