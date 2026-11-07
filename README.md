# journal-harvester — 整刊批量文献下载（多人协作版）

把"某本期刊近 N 年全部文章"清成清单，再按 **OA 免费通道 → 校园网机构订阅（真浏览器）**
两层漏斗批量下 PDF，manifest 全程记账、断点续跑。多人协作：主控枚举+预标记一次，
大家按刊**领任务**，各自在本机下载，定期打包回传统一汇总。

## 新人上手（10 分钟）

1. **装 Python 3.11+**（勾选 Add to PATH），然后：
   ```
   pip install requests openpyxl
   ```
   要跑订阅层（CDP 真浏览器）再加：`pip install playwright`
2. **拿工具**：clone 本仓库，或解压主控发给你的 zip。
3. **配置**：复制 `config.example.toml` 为 `config.toml`，把 `me.name` 改成你的名字。
4. **领刊**：
   ```
   harvest list              REM 看有哪些刊、谁领了
   harvest claim 7           REM 按编号领，或 harvest claim Genetics
   ```
   领完 `git commit` + `push`（冲突 = 别人先领了，`git pull` 后重选）。
5. **下载**：
   ```
   harvest run               REM OA 层：自动、无需浏览器、挂着就行
   harvest report            REM 跑完出汇总，看 ok/paywalled/failed
   ```
6. **回传**：
   ```
   harvest pack              REM 把你名下所有刊的 PDF 打 zip + sha256
   ```
   zip 放到主控指定的地方（网盘/拷盘），主控统一入库文献库。

## 订阅层（CDP 真浏览器）

OA 层拿不到的（Elsevier/Wiley/Elsevier托管刊等），用你的**校园网/VPN + 真 Edge** 下：

1. 完全退出 Edge，然后启动带调试端口的独立 profile（Edge 136+ 不允许默认 profile 开 CDP）：
   ```
   "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir=%USERPROFILE%\harvest_edge_profile --no-first-run
   ```
2. 在这个 Edge 里随便访问一篇 ScienceDirect 文章，确认页头显示"Sun Yat-Sen University"
   （机构认证生效）；如弹人机验证，手动点一次，cookie 会留在这个 profile 里。
3. `harvest run --layer cdp`（保持这个 Edge 开着，低速 4-8s/篇；每 50 篇看一眼有没有验证码）。

## 中文刊

走万方期刊页只读枚举 + 知网页面批量下载（不要写脚本自动点知网！风控会封）：

- 枚举：`python harvester\legacy\wanfang_crawl.py --out-dir <目录> --journal "畜牧兽医学报:xmsyxb:遗传育种"`
  （万方 id 规律：拼音缩写 6 字母；栏目名各刊不同，试错即知）
- 知网题录导出：检索页全选 → 导出 EndNote（每批 500）→ `python harvester\legacy\cnki_manifest.py parse --export xxx.txt --out manifest.csv`
- 知网批量下载：结果页分批勾选（每批 ≤50）→ 下载 zip 解压 → `cnki_manifest.py match` 回填

## 检索（先看量再决定下多少）

**英文刊用英文关键词，中文刊用中文关键词**（检索按题名匹配）：

```
REM 两组词，组内 OR、组间 AND：题名含 (genetic|breeding) 且含 (pig|cattle)
harvest search --topic "genetic,breeding,development" --animal "pig,cattle,sheep" --all

REM 满意了就打标（写 topic 列），之后只下命中行：
harvest search --topic "..." --animal "..." --all --tag
harvest run --topic-only

REM 导出命中清单人工过目：
harvest search --topic "..." --animal "..." --all --out hits.csv
```

`--journal Animals` 精确匹配单刊；`--match any` 改为任一组命中即可；中文刊示例：
`harvest search --topic "遗传,育种,发育" --animal "猪,牛,羊" --journal cn_yc`。

## 密钥（各自申请，绝不入库）

| 密钥 | 谁需要 | 怎么申请 | 放哪 |
|---|---|---|---|
| OpenAlex API key | 只有主控（预标记用） | openalex.org 注册（免费，送 $1/天额度） | `secrets/api_key.txt` 里写一行：`openalex 你的key` |

`secrets/` 目录已在 .gitignore 里。**任何人不得把 key 提交进 git、贴进群、写进文档。**

## 主控操作

```
harvest enumerate --issn 0888-7543 --out slug            REM 单刊枚举（近10年）
harvest precheck                                          REM 全 corpus 预标记（需 OpenAlex key）
harvest merge                                             REM 收各人 zip 后统一校验入库（人工+03脚本）
```

注意：**per-ISSN 视图会撒谎**（期刊换登记记录后旧 ISSN 停更，返回 0 或偏少）——
枚举完先看量级；量级不对就用 Europe PMC 反查一篇近年文章看它真实挂的 ISSN。
刊名模糊匹配易错配（Animal→Animals），拿不准直接给 ISSN。

## 目录结构

```
corpus/<任务名>/manifests/   主控产出并入库的清单（含 OA 预标记 oa_url 列）
workspace/<任务名>/pdfs/     你下载的 PDF（按刊分目录，不入库）
workspace/<任务名>/outbox/   harvest pack 的 zip（回传用）
ASSIGNMENTS.toml             分配表（入库）
config.toml                  你的个人配置（不入库）
```
