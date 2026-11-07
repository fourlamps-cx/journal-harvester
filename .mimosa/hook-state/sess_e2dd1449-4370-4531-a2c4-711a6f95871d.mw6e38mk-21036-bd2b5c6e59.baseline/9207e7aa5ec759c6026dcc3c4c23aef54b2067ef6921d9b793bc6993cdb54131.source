# -*- coding: utf-8 -*-
"""下载漏斗：OA 通道 -> 订阅会话 -> XML 转录兜底。按 manifest 状态幂等续跑。

通道顺序（命中即停，行为表继承 wechat-paper-harvester 2026-10 实测）：
  1. Europe PMC PDF         2. Unpaywall        3. 出版商直链表（OA 可直下）
  4. 订阅直下（--session cookies 时，对 Wiley/Elsevier/OUP/Springer 生效）
  5. Europe PMC 全文 XML -> 本地渲染 PDF（--xml-fallback，需 pymupdf；标「全文转录版」）

铁律：所有声称成功的下载都过 %PDF- magic 校验；429 退避不硬冲；同域连续 3 次
403 即拉黑本域；限速 2.5~5.5s 抖动。不使用打码服务/代理轮换。
"""
import argparse
import csv
import http.cookiejar
import html
import json
import os
import re
import sys
import time
import urllib.parse

import requests

try:
    sys.stdout.reconfigure(errors="replace")  # 跟随本机控制台编码，GBK 下不乱码、异常字符不崩
except Exception:
    pass

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UNPAYWALL = "https://api.unpaywall.org/v2"
FIELDS = ["source", "doi", "pii", "title", "year", "month", "first_author",
          "journal", "volume", "issue", "pages", "type", "url",
          "oa_channel", "status", "file", "note"]


def contact_email():
    v = os.environ.get("UNPAYWALL_EMAIL", "").strip()
    if not v and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, "UNPAYWALL_EMAIL")[0]).strip()
        except Exception:
            v = ""
    return v


def load_session(path):
    """cookies.txt（Netscape）或 JSON 数组格式 -> RequestsCookieJar。"""
    with open(path, encoding="utf-8-sig") as f:
        txt = f.read().lstrip()
    jar = requests.cookies.RequestsCookieJar()
    if txt.startswith("[") or txt.startswith("{"):
        data = json.loads(txt)
        items = data if isinstance(data, list) else data.get("cookies", [])
        for c in items:
            jar.set(c.get("name"), c.get("value"),
                    domain=c.get("domain"), path=c.get("path", "/"))
        print(f"[session] JSON cookies {len(items)} 条")
    else:
        cj = http.cookiejar.MozillaCookieJar()
        cj.load(path, ignore_discard=True, ignore_expires=True)
        for c in cj:
            jar.set_cookie(c)
        print(f"[session] Netscape cookies {len(cj)} 条")
    return jar


def is_pdf(b):
    return b[:5] == b"%PDF-" or (len(b) > 20000 and b"%PDF-" in b[:2048])


def safe_name(year, fam, title):
    t = re.sub(r"\s+", " ", title or "").strip()
    s = f"{year or 'NA'}_{fam or 'NA'}_{t[:60]}".strip()
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "", s).rstrip(". ")


class Fetcher:
    def __init__(self, min_delay, max_delay, session_path, env_proxy):
        self.s = requests.Session()
        self.s.trust_env = env_proxy
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if session_path:
            self.s.cookies.update(load_session(session_path))
        self.lo, self.hi = min_delay, max_delay
        self.host_fails = {}   # 连续 403 计数，>=3 拉黑本域
        self.email = contact_email()

    def sleep(self):
        time.sleep(self.lo + (self.hi - self.lo) * (os.urandom(1)[0] / 255.0))

    def blocked(self, host):
        return self.host_fails.get(host, 0) >= 3

    def get(self, url, note_ctx="", stream=False, timeout=120):
        """返回 (resp|None, reason)。403 计入本域拉黑，429 抛出由上层退避。"""
        from urllib.parse import urlsplit
        host = urlsplit(url).netloc
        if self.blocked(host):
            return None, "host-blacklisted"
        self.sleep()
        try:
            r = self.s.get(url, timeout=timeout, stream=stream)
        except requests.RequestException as e:
            return None, f"net:{type(e).__name__}"
        if r.status_code == 429:
            raise RateLimited(url)
        if r.status_code in (403, 401):
            self.host_fails[host] = self.host_fails.get(host, 0) + 1
            if self.host_fails[host] >= 3:
                print(f"[blacklist] {host} 连续 403>=3，本域拉黑", flush=True)
            return None, f"http-{r.status_code}"
        if r.status_code != 200:
            return None, f"http-{r.status_code}"
        return r, ""


class RateLimited(Exception):
    def __init__(self, url):
        super().__init__(url)
        self.url = url


def _short_sleep():
    """轻量 JSON 查询之间的礼貌间隔（重请求走 Fetcher.get 的 2.5~5.5s）。"""
    time.sleep(0.8 + os.urandom(1)[0] / 255.0)


# ---------- 通道 1/2：EPMC PDF、Unpaywall ----------

def epmc_pdf_doi(f, doi):
    """Europe PMC：按 DOI 查，取 Open access/Free 的 pdf 链接。"""
    _short_sleep()
    try:
        r = f.s.get(EPMC_SEARCH, params={"query": f'DOI:"{doi}"', "format": "json",
                                         "resultType": "core", "pageSize": 1},
                    timeout=60)
        hits = r.json().get("resultList", {}).get("result") or []
    except Exception:
        return None, "epmc-parse"
    if not hits:
        return None, "not-in-epmc"
    for u in (hits[0].get("fullTextUrlList") or {}).get("fullTextUrl") or []:
        if u.get("documentStyle") == "pdf" and \
                u.get("availability") in ("Open access", "Free"):
            url = u.get("url") or ""
            # europepmc.org 自家的 PDF 端点对脚本永远 403（harvester 实测），只留 PMC/仓库直链
            if url and "europepmc.org" not in url:
                return url, ""
    return None, "epmc-no-free-pdf"


def unpaywall_pdf(f, doi):
    if not f.email:
        return None, "no-email"
    # email 必须显式拼进 URL（f.get 不带 params）；漏了会 HTTP 422 整通道静默失效
    r, reason = f.get(f"{UNPAYWALL}/{doi}?email={urllib.parse.quote(f.email)}",
                      timeout=40)
    if r is None:
        return None, reason or "unpaywall-fail"
    try:
        data = r.json()
    except Exception:
        return None, "unpaywall-parse"
    locs = []
    if data.get("best_oa_location"):
        locs.append(data["best_oa_location"])
    locs += data.get("oa_locations") or []
    for loc in locs:
        u = loc.get("url_for_pdf")
        if u:
            return u, ""
    return None, "unpaywall-no-pdf"


# ---------- 通道 3/4：出版商直链表 ----------

def direct_urls(row):
    """按 DOI 前缀构造候选直链（bioRxiv/medRxiv 共用 10.1101，两主机都试）。"""
    doi = row["doi"]
    d = (doi or "").lower()
    suffix = d.split("/", 1)[1] if "/" in d else ""
    if d.startswith("10.1038/"):
        return [f"https://www.nature.com/articles/{suffix}.pdf"]
    if d.startswith("10.1186/"):
        slug = re.sub(r"[^a-z0-9]", "", (row.get("journal") or "").lower())
        if not slug:
            return []
        # counter/pdf/{doi} 对部分刊要 .pdf 后缀、部分不要（2026-11 实测两种都有），都试
        base = f"https://{slug}.biomedcentral.com/counter/pdf/{doi}"
        return [base, base + ".pdf"]
    if d.startswith("10.1101/") or d.startswith("10.64898/"):
        return [f"https://www.biorxiv.org/content/{doi}v1.full.pdf",
                f"https://www.medrxiv.org/content/{doi}v1.full.pdf"]
    if d.startswith("10.48550/"):
        aid = suffix.replace("arxiv.", "")
        return [f"https://arxiv.org/pdf/{aid}"] if aid else []
    if d.startswith("10.1007/"):
        return [f"https://link.springer.com/content/pdf/{doi}"]          # 订阅内有效
    if d.startswith("10.1002/") or d.startswith("10.1111/"):
        return [f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"]  # 需会话
    if d.startswith("10.1016/") and pii:
        return [("https://www.sciencedirect.com/science/article/pii/"
                 f"{pii}/pdfft?isDTMRedir=true&download=true")]          # 需会话+PII
    if d.startswith("10.3390/"):
        # MDPI：DOI 后缀 = 刊名缩写+卷+期+文号（agriengineering8100430）。
        # 脚本裸访问 403（云盾，实测 2026-11），无会话时由禁区表跳过，
        # 有会话/CDP 通道才有意义。
        m = re.match(r"^([a-z]+?)(\d+)$", suffix)
        vol = (row.get("volume") or "").strip()
        iss = (row.get("issue") or "").strip()
        issn = (row.get("source") or "").split(":")[-1]
        if m and vol and iss and m.group(2).startswith(f"{vol}{iss}"):
            no = m.group(2)[len(vol) + len(iss):].lstrip("0") or "0"
            out = []
            if issn:
                out.append(f"https://www.mdpi.com/{issn}/{vol}/{iss}/{no}/pdf")
            out.append(f"https://www.mdpi.com/{m.group(1)}/{vol}/{iss}/{no}/pdf")
            return out
        return []
    if d.startswith("10.3389/"):
        # Frontiers 全 OA，直链实测可下（2026-11）
        return [f"https://www.frontiersin.org/articles/{doi}/pdf"]
    if d.startswith("10.3168/") and row.get("pii"):
        # JDS 实际由 Elsevier 托管，走 SD pdfft（需会话；无会话命中禁区表）
        return [("https://www.sciencedirect.com/science/article/pii/"
                 f"{row['pii']}/pdfft?isDTMRedir=true&download=true")]
    if d.startswith("10.1093/"):
        return []  # OUP 路径结构不稳定，直链不猜，走会话/EPMC 兜底
    return []


# 无会话时已知 403/云盾硬拦的域，直接不试（继承 harvester 实测表 + 2026-11 补测）
NO_SESSION_HOPELESS = ("onlinelibrary.wiley.com", "www.sciencedirect.com",
                       "academic.oup.com", "www.mdpi.com")


def try_direct(f, row, has_session):
    cands = direct_urls(row)
    if not cands:
        return None, "no-direct-url"
    last = "no-direct-url"
    for u in cands:
        host = u.split("/")[2]
        if not has_session and host in NO_SESSION_HOPELESS:
            last = "paywalled-host"
            continue
        if f.blocked(host):
            last = "host-blacklisted"
            continue
        r, reason = f.get(u, stream=True)
        if r is None:
            last = reason or "direct-fail"
            continue
        try:
            chunks = []
            for ch in r.iter_content(65536):
                chunks.append(ch)
                if sum(len(c) for c in chunks) > 60 * 1024 * 1024:
                    return None, "too-large"
            body = b"".join(chunks)
        finally:
            r.close()
        if is_pdf(body[:2048]):
            return body, ""
        last = "not-pdf"
    return None, last


# ---------- 通道 5：EPMC XML 转录兜底 ----------

def xml_fallback(f, doi, out_path):
    try:
        import fitz  # pymupdf
    except ImportError:
        return "no-pymupdf"
    _short_sleep()
    try:
        r = f.s.get(EPMC_SEARCH, params={"query": f'DOI:"{doi}"',
                                         "format": "json", "pageSize": 1},
                    timeout=60)
        hits = r.json().get("resultList", {}).get("result") or []
    except Exception:
        return "epmc-parse"
    pmcid = hits[0].get("pmcid") if hits and hits[0].get("pmcid") else None
    if not pmcid:
        return "no-pmcid"
    time.sleep(1.5)
    try:
        x = f.s.get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/"
                    f"{pmcid}/fullTextXML", timeout=90)
    except requests.RequestException:
        return "xml-net"
    if x.status_code != 200 or "<body" not in x.text:
        return f"xml-http-{x.status_code}"
    txt = x.text
    paras = [re.sub(r"<[^>]+>", "", m) for m in
             re.findall(r"<p[^>]*>(.*?)</p>", txt, re.S | re.I)]
    paras = [html.unescape(p).strip() for p in paras if p.strip()]
    if not paras:
        return "xml-empty"
    doc = fitz.open()
    page, y = None, 0.0
    line_h, max_lines = 13.5, 48
    for p in paras:
        for chunk in _wrap(p, 92):
            if page is None or y > max_lines * line_h:
                page = doc.new_page(width=595, height=842)
                y = 40
            page.insert_text((40, y), chunk, fontsize=10, fontname="helv")
            y += line_h
    doc.save(out_path)
    doc.close()
    return ""


def _wrap(s, width):
    out, cur, n = [], [], 0
    for w in s.split():
        if n + len(w) + 1 > width and cur:
            out.append(" ".join(cur))
            cur, n = [w], len(w)
        else:
            cur.append(w)
            n += len(w) + 1
    if cur:
        out.append(" ".join(cur))
    return out


# ---------- 主循环 ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--session", help="cookies.txt(Netscape) 或 JSON cookies")
    ap.add_argument("--xml-fallback", action="store_true",
                    help="拿不到 OA/订阅 PDF 时用 EPMC 全文 XML 渲染转录版")
    ap.add_argument("--premarked-only", action="store_true",
                    help="只处理 00_precheck_openalex.py 标了 oa_url 的行，跳过逐篇 epmc/unpaywall（全量生产模式）")
    ap.add_argument("--only-topic", action="store_true",
                    help="只处理 topic=hit 的行（配合 harvester filter 命令）")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--retry-paywalled", action="store_true")
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--min-delay", type=float, default=2.5)
    ap.add_argument("--max-delay", type=float, default=5.5)
    ap.add_argument("--env-proxy", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    with open(args.manifest, encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        assert set(FIELDS) <= set(rdr.fieldnames), \
            f"manifest 缺列：{set(FIELDS) - set(rdr.fieldnames)}"
        fnames = list(rdr.fieldnames)   # 保留全部列（precheck 会加 oa_url）
        rows = list(rdr)
    todo = [r for r in rows if r["status"] == "pending"
            or (args.retry_failed and r["status"] == "failed")
            or (args.retry_paywalled and r["status"] == "paywalled")]
    if args.premarked_only:
        todo = [r for r in todo if (r.get("oa_url") or "").strip()]
    if args.only_topic:
        todo = [r for r in todo if (r.get("topic") or "") == "hit"]
    if args.max_items:
        todo = todo[:args.max_items]
    print(f"共 {len(rows)} 行，本轮处理 {len(todo)} 行"
          + ("（仅预标记 OA）" if args.premarked_only else ""), flush=True)

    f = Fetcher(args.min_delay, args.max_delay, args.session, args.env_proxy)
    has_session = bool(args.session)
    tally = {"ok": 0, "paywalled": 0, "failed": 0}
    done = 0

    def save():
        with open(args.manifest, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fnames)
            w.writeheader()
            w.writerows(rows)

    for row in todo:
        done += 1
        doi = row["doi"]
        base = safe_name(row["year"], row["first_author"], row["title"])
        path = os.path.join(args.out_dir, base + ".pdf")
        n = 2
        while os.path.exists(path):
            path = os.path.join(args.out_dir, f"{base}-{n}.pdf")
            n += 1

        status, channel, trace = "failed", "", []
        try:
            if not doi:
                trace.append("no-doi")
            else:
                if args.premarked_only:
                    channels = [("openalex-pre",
                                 lambda: (row.get("oa_url") or "", "")),
                                ("unpaywall", lambda: unpaywall_pdf(f, doi))]
                                # OpenAlex 存的直链会过期(404)，回退 Unpaywall 再判死
                else:
                    channels = [("epmc-pdf", lambda: epmc_pdf_doi(f, doi)),
                                ("unpaywall", lambda: unpaywall_pdf(f, doi))]
                for name, fn in channels:
                    u, reason = fn()
                    if u:
                        r, rreason = f.get(u, stream=True)
                        if r is not None:
                            body = r.content
                            r.close()
                            if is_pdf(body[:2048]) or is_pdf(body[-2048:]):
                                with open(path, "wb") as pf:
                                    pf.write(body)
                                status, channel = "ok", name
                                break
                            reason = "not-pdf"
                        else:
                            reason = rreason or "fetch-fail"
                    trace.append(f"{name}:{reason}")
                    if isinstance(reason, str) and reason.startswith("net:"):
                        break
                if status != "ok":
                    body, reason = try_direct(f, row, has_session)
                    if body:
                        with open(path, "wb") as pf:
                            pf.write(body)
                        status = "ok"
                        channel = ("session-" if has_session else "direct-") \
                            + doi.split("/")[0]
                    else:
                        trace.append(f"direct:{reason}")
                if status != "ok" and args.xml_fallback:
                    err = xml_fallback(f, doi, path)
                    if not err:
                        status, channel = "ok", "epmc-xml-transcript"
                        trace.append("全文转录版(非出版商排版)")
                    else:
                        trace.append(f"xml:{err}")
        except RateLimited as e:
            print(f"[429] 退避 30s：{e.url}", flush=True)
            time.sleep(30)
            status = "failed"
            trace = ["http-429"]
        note = "|".join(t for t in trace if t)
        if status == "ok" and not channel:
            status = "failed"
        if status != "ok" and ("paywalled" in note or "host-blacklisted" in note):
            status = "paywalled"
        if status != "ok":
            note = (note or "(无原因)") + f" landing=https://doi.org/{doi}"
        row.update(status=status, oa_channel=channel or row["oa_channel"],
                   file=os.path.basename(path) if status == "ok" else "",
                   note=note.strip())
        tally[status] += 1
        print(f"[{done}/{len(todo)}] {status:9s} {channel or note or '':34s} {doi}",
              flush=True)
        if done % 10 == 0:
            save()
    save()

    print(f"\n== 完成：ok={tally['ok']} paywalled={tally['paywalled']} "
          f"failed={tally['failed']}")
    print("下一步：python 03_verify_and_report.py --manifest ... --out-dir pdfs")


if __name__ == "__main__":
    main()
