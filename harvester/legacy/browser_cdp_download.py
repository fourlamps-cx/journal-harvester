# -*- coding: utf-8 -*-
r"""CDP 真浏览器下载器（第 3b 层，实验性）：attach 到你亲手启动的 Chrome。

适用场景：02_download_pdfs.py 带 cookies.txt 仍被 Cloudflare/PerimeterX 拦
（ScienceDirect/Wiley 常见）。真浏览器 = 真 TLS 指纹 + 完整 cookie jar。

使用步骤（一次性准备）：
  1. pip install playwright          # 只装库，不需要 playwright install
  2. 校园网/VPN 在线，全新启动一个带调试端口的 Chrome（必须先退出所有 Chrome 进程）：
       "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
         --remote-debugging-port=9222 ^
         --user-data-dir=%USERPROFILE%\harvest_browser_profile ^
         --no-first-run
     （Chrome 136+ 不允许在默认 profile 上开 CDP，所以要独立 user-data-dir；
       首次打开后在里面访问目标出版商，确认能看到已订阅/能下 PDF，之后这个
       profile 的登录态会一直保留，不用每次重登。）
  3. python browser_cdp_download.py --manifest manifest.csv --out-dir pdfs \
       --only paywalled --max 50
  低速 4~8s/篇；每 50 篇建议看一眼 Chrome 窗口有没有弹验证码，弹了手动点掉再继续。
"""
import argparse
import csv
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(errors="replace")  # 跟随本机控制台编码，GBK 下不乱码、异常字符不崩
except Exception:
    pass

FIELDS = ["source", "doi", "pii", "title", "year", "month", "first_author",
          "journal", "volume", "issue", "pages", "type", "url",
          "oa_channel", "status", "file", "note"]


def safe_name(year, fam, title):
    t = re.sub(r"\s+", " ", title or "").strip()
    s = f"{year or 'NA'}_{fam or 'NA'}_{t[:60]}".strip()
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "", s).rstrip(". ")


def build_url(row):
    doi = row["doi"]
    d = (doi or "").lower()
    suffix = d.split("/", 1)[1] if "/" in d else ""
    if d.startswith("10.1016/") and row.get("pii"):
        return ("https://www.sciencedirect.com/science/article/pii/"
                f"{row['pii']}/pdfft?isDTMRedir=true&download=true")
    if d.startswith("10.1002/") or d.startswith("10.1111/"):
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    if d.startswith("10.1007/"):
        return f"https://link.springer.com/content/pdf/{doi}"
    if d.startswith("10.3390/"):
        # MDPI：DOI 后缀 = 刊名缩写+卷+期+文号；脚本裸访问 403，必须真浏览器
        m = re.match(r"^([a-z]+?)(\d+)$", suffix)
        vol = (row.get("volume") or "").strip()
        iss = (row.get("issue") or "").strip()
        issn = (row.get("source") or "").split(":")[-1]
        if m and vol and iss and m.group(2).startswith(f"{vol}{iss}"):
            no = m.group(2)[len(vol) + len(iss):].lstrip("0") or "0"
            if issn:
                return f"https://www.mdpi.com/{issn}/{vol}/{iss}/{no}/pdf"
            return f"https://www.mdpi.com/{m.group(1)}/{vol}/{iss}/{no}/pdf"
        return None
    if d.startswith("10.1093/"):
        return None  # OUP 路径不稳定，交人工
    if d.startswith("10.1038/"):
        return f"https://www.nature.com/articles/{suffix}.pdf"
    return None


def is_pdf(b):
    return b[:5] == b"%PDF-" or (len(b) > 20000 and b"%PDF-" in b[:2048])


# 无会话脚本被硬拦、只能走真浏览器的域（OA 轮会把这些行留成 pending 或标 paywalled）
BLOCKED_HOSTS = ("www.mdpi.com",)


def oa_url_host_blocked(row):
    u = (row.get("oa_url") or "").strip()
    if not u:
        return False
    try:
        return u.split("/")[2] in BLOCKED_HOSTS
    except IndexError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--cdp", default="http://127.0.0.1:9222")
    ap.add_argument("--only", default="paywalled",
                    choices=["paywalled", "pending", "all"],
                    help="paywalled(默认)会自动并入 oa_url 指向禁域(如 MDPI)的 pending 行")
    ap.add_argument("--max", type=int, default=50)
    ap.add_argument("--min-delay", type=float, default=4.0)
    ap.add_argument("--max-delay", type=float, default=8.0)
    ap.add_argument("--auto", action="store_true",
                    help="每 50 篇不暂停等人看验证码（无人值守时用，风险自担）")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("[fatal] 需要 playwright：pip install playwright（无需 playwright install）")

    with open(args.manifest, encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        rows = list(rdr)
    want = {"all": None, "paywalled": {"paywalled"}, "pending": {"pending"}}[args.only]
    todo = [r for r in rows if want is None or r["status"] in want]
    if args.only in ("paywalled", "auto"):
        # paywalled 之外，把「OA 直链指向脚本禁域」的 pending 行（MDPI 类）也交给真浏览器
        todo += [r for r in rows
                 if r["status"] == "pending" and oa_url_host_blocked(r)]
    todo = [r for r in todo if build_url(r)][:args.max]
    if not todo:
        print("没有可处理的行（注意：OUP 无稳定直链，不在本脚本范围）")
        return
    print(f"本轮 {len(todo)} 篇（{args.only}）", flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp, timeout=15000)
        except Exception as e:
            sys.exit(f"[fatal] 连不上 CDP {args.cdp}：{e}\n"
                     "先按脚本头部注释启动带 --remote-debugging-port 的 Chrome")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        tally = {"ok": 0, "failed": 0}
        for i, row in enumerate(todo, 1):
            url = build_url(row)
            base = safe_name(row["year"], row["first_author"], row["title"])
            dest = os.path.join(args.out_dir, base + ".pdf")
            n = 2
            while os.path.exists(dest):
                dest = os.path.join(args.out_dir, f"{base}-{n}.pdf")
                n += 1
            status, note = "failed", ""
            try:
                # 路线1：context.request（共享 cookie，速度快）
                resp = ctx.request.get(url, timeout=90000)
                body = resp.body()
                if resp.ok and is_pdf(body[:2048]):
                    with open(dest, "wb") as f:
                        f.write(body)
                    status, note = "ok", "cdp-request"
                # 路线2：真页面导航 + 下载事件（真 Chrome 网络栈）
                if status != "ok":
                    with page.expect_download(timeout=90000) as dl_info:
                        try:
                            page.goto(url, timeout=90000, wait_until="commit")
                        except Exception:
                            pass  # PDF 触发下载时 goto 常抛 net::ERR_ABORTED，正常
                    dl = dl_info.value
                    dl.save_as(dest)
                    with open(dest, "rb") as f:
                        head = f.read(2048)
                    if is_pdf(head):
                        status, note = "ok", "cdp-download"
                    else:
                        os.rename(dest, dest + ".badhtml")
                        note = "not-pdf"
            except Exception as e:
                note = f"cdp:{type(e).__name__}"
                if "403" in str(e) or "forbidden" in str(e).lower():
                    note = "cdp-403"
            row.update(status=status, oa_channel=note if status == "ok" else row["oa_channel"],
                       file=os.path.basename(dest) if status == "ok" else "",
                       note=("" if status == "ok" else note))
            tally[status] += 1
            print(f"[{i}/{len(todo)}] {status:8s} {note:14s} {row['doi']}", flush=True)
            if i % 10 == 0:
                _save(rows, args.manifest)
            time.sleep(args.min_delay + (args.max_delay - args.min_delay)
                       * (os.urandom(1)[0] / 255.0))
            if i % 50 == 0 and not args.auto:
                print(">> 已 50 篇：看一眼 Chrome 有没有弹验证码，点掉后按回车继续…",
                      flush=True)
                input()
        _save(rows, args.manifest)
        print(f"== cdp 完成：ok={tally['ok']} failed={tally['failed']} -> "
              f"重跑 03_verify_and_report.py 汇总")


def _save(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
