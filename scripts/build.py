#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox / OK影视 自建自动更新接口
每天做三件事：
1) 拉取 sources.txt 里列出的上游配置；
2) 以「主源」为骨架合并其它源的独立站点（按 api 去重）；
3) 实测站点接口是否活着，剔除死链，重算 spider 的 md5，输出 tvbox.json。
产物：tvbox.json（配置本身）、meta.json（本次构建报告）
"""

import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "okhttp/3.15"
TIMEOUT = 20
PROBE_TIMEOUT = 7
PROBE_WORKERS = 40
MAX_SITES = 900

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def log(*a):
    print(*a, flush=True)


def http_get(url, timeout=TIMEOUT, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "ignore")


def strip_json_comments(text):
    """去掉 // 开头的整行注释和 BOM，兼容部分配置里的说明行。"""
    text = text.lstrip("\ufeff")
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("//")]
    return "\n".join(lines).strip()


def load_sources():
    path = os.path.join(ROOT, "sources.txt")
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) < 2:
                continue
            name, url = parts[0], parts[1]
            base = parts[2] if len(parts) > 2 and parts[2] else None
            if not base:
                base = url.rsplit("/", 1)[0] + "/"
            out.append({"name": name, "url": url, "base": base})
    return out


def normalize_config(cfg, base):
    """就地补全 sites / lives / parses 里的相对地址，并去掉无效占位项。"""
    cfg["sites"] = [normalize_site(s, base) for s in (cfg.get("sites") or []) if isinstance(s, dict)]

    lives = []
    for item in cfg.get("lives") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and ("://" not in url):
            item = dict(item)
            item["url"] = abs_url(base, url)
        if isinstance(item.get("epg"), str) and item["epg"].startswith("./"):
            item = dict(item)
            item["epg"] = abs_url(base, item["epg"])
        lives.append(item)
    cfg["lives"] = lives

    parses = []
    for item in cfg.get("parses") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if "://" not in url:
            # Demo / 占位符之类的无效解析，直接丢弃
            continue
        parses.append(item)
    cfg["parses"] = parses
    return cfg


def fetch_config(src):
    try:
        text = strip_json_comments(http_get(src["url"], timeout=TIMEOUT))
        data = json.loads(text)
        if not isinstance(data, dict) or not data.get("sites"):
            raise ValueError("no sites")
        return src, normalize_config(data, src["base"])
    except Exception as e:
        log(f"[跳过] {src['name']}: {e}")
        return src, None


def abs_url(base, path):
    """把 ./jar/pg.jar、jar/x.jar、http://... 统一成绝对地址。"""
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("./"):
        path = path[2:]
    return urllib.parse.urljoin(base, path)


def split_md5(spider):
    """spider 形如 url;md5;xxxx → (url, md5)。"""
    if not spider:
        return None, None
    raw = spider if isinstance(spider, str) else ""
    parts = raw.split(";")
    url = parts[0].strip()
    md5 = None
    for i, p in enumerate(parts):
        if p.strip().lower() == "md5" and i + 1 < len(parts):
            md5 = parts[i + 1].strip()
    return url, md5


def spider_key(spider):
    """判断两个源的 spider 是否为同一个 jar（同 URL 或同 md5）。"""
    url, md5 = split_md5(spider)
    return (url or "", md5 or "")


def normalize_site(site, base):
    """把站点里的相对路径补全成绝对地址，返回新 dict。"""
    s = dict(site)
    api = s.get("api")
    if isinstance(api, str):
        a = api.strip()
        if a.startswith("./") or a.startswith("../") or (
            "." in a.split("/")[-1] and not a.startswith(("http://", "https://", "csp_", "py_"))
        ):
            s["api"] = abs_url(base, a)
    ext = s.get("ext")
    if isinstance(ext, str) and ext.strip().startswith("./"):
        s["ext"] = abs_url(base, ext)
    return s


def site_dedupe_key(s):
    """api + ext 一起判重：同一 py 脚本配不同 ext 是不同站点，不能合并掉。"""
    api = str(s.get("api") or "").strip().rstrip("/").lower()
    ext = str(s.get("ext") or "").strip()
    if api:
        return "api:" + api + "|" + ext[:60]
    return "name:" + str(s.get("name") or "").strip().lower()


def probe_one(site):
    """实测站点接口。返回 (key, ok, note)。"""
    key = site_dedupe_key(site)
    api = str(site.get("api") or "")
    if not api:
        return key, False, "no-api"
    if api.startswith("csp_") or api.startswith("py_"):
        # 依赖 spider jar 的写法（csp_x），本地无法验证，保留由 jar 处理
        return key, True, "jar-site"
    if not api.startswith(("http://", "https://")):
        return key, True, "skip"
    if api.endswith(".py") or api.endswith(".js") or api.endswith(".json"):
        try:
            http_get(api, timeout=PROBE_TIMEOUT)
            return key, True, "script"
        except Exception:
            return key, False, "script-fail"

    probe = api + ("&" if "?" in api else "?") + "ac=list"
    for _ in range(2):
        try:
            body = http_get(probe, timeout=PROBE_TIMEOUT)
            if '"class"' in body or '"list"' in body or "vod_" in body:
                return key, True, "cms"
            return key, False, "cms-empty"
        except Exception:
            time.sleep(1)
    return key, False, "cms-timeout"


def main():
    srcs = load_sources()
    log(f"上游 {len(srcs)} 个")

    results = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for src, data in ex.map(fetch_config, srcs):
            if data:
                results.append((src, data))
    if not results:
        log("所有上游都拉取失败，保留上一版 tvbox.json，不发布空配置")
        return 1

    # 主源 = sources.txt 里第一个成功的源，作为骨架（spider / lives / parses 基准）
    main_src, main_cfg = results[0]
    log(f"主源: {main_src['name']}")

    main_base = main_src["base"]
    spider_url, _old_md5 = split_md5(main_cfg.get("spider"))
    spider_abs = abs_url(main_base, spider_url) if spider_url else None
    main_spider_key = spider_key(main_cfg.get("spider"))

    sites, seen = [], set()

    def add_sites(cfg, base, allow_csp):
        added = 0
        for s in cfg.get("sites") or []:
            if not isinstance(s, dict):
                continue
            api = str(s.get("api") or "")
            if (not allow_csp) and api.startswith("csp_"):
                continue
            ns = normalize_site(s, base)
            k = site_dedupe_key(ns)
            if k in seen:
                continue
            seen.add(k)
            sites.append(ns)
            added += 1
        return added

    add_sites(main_cfg, main_base, allow_csp=True)

    stat = {main_src["name"]: len(main_cfg.get("sites") or [])}
    for src, cfg in results[1:]:
        same_spider = spider_key(cfg.get("spider")) == main_spider_key
        n = add_sites(cfg, src["base"], allow_csp=same_spider)
        stat[src["name"]] = n
        log(f"  + {src['name']}: 并入 {n} 个站点{'（同 jar）' if same_spider else '（仅独立源）'}")

    if len(sites) > MAX_SITES:
        sites = sites[:MAX_SITES]

    # ---- 探活 ----
    log(f"合并后站点 {len(sites)} 个，开始实测接口…")
    ok_keys, unknown_keys = set(), set()
    notes = {}
    with cf.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        futs = [ex.submit(probe_one, s) for s in sites]
        for f in cf.as_completed(futs):
            k, ok, note = f.result()
            notes[note] = notes.get(note, 0) + 1
            if ok:
                ok_keys.add(k)
            elif note != "no-api":
                # 可能是海外探测环境够不到的国内站，保留但排到后面
                unknown_keys.add(k)

    before = len(sites)
    good = [s for s in sites if site_dedupe_key(s) in ok_keys]
    rest = [s for s in sites if site_dedupe_key(s) in unknown_keys]
    dropped = before - len(good) - len(rest)
    sites = good + rest
    log(f"探活结果: {notes}")
    log(f"实测可用 {len(good)}，未验证保留 {len(rest)}，剔除 {dropped}（合计 {len(sites)}）")

    # ---- spider：下载 jar 重算 md5 ----
    # 可选：SPIDER_MIRROR 环境变量（如 https://gh-proxy.com/）会让写进配置的 jar 地址
    # 走加速镜像，盒子下载更快；md5 仍按真实文件计算，不受影响。
    spider_field = None
    jar_info = None
    if spider_abs:
        try:
            blob = http_get(spider_abs, timeout=30, binary=True)
            md5 = hashlib.md5(blob).hexdigest()
            mirror = os.environ.get("SPIDER_MIRROR", "").strip()
            out_url = (mirror + spider_abs) if mirror else spider_abs
            spider_field = f"{out_url};md5;{md5}"
            jar_info = {"url": out_url, "origin": spider_abs, "md5": md5, "size": len(blob)}
            log(f"jar 有效: {len(blob)} 字节 md5={md5}")
        except Exception as e:
            log(f"jar 下载失败({e})，改为只写地址不写 md5")
            spider_field = spider_abs

    # ---- 合并 lives / parses / flags ----
    def merge_list(field, key_fields=("name", "url")):
        out, seen_k = [], set()
        for _src, cfg in results:
            for item in cfg.get(field) or []:
                if not isinstance(item, dict):
                    continue
                k = tuple(str(item.get(x) or "").strip().lower() for x in key_fields)
                if k in seen_k:
                    continue
                seen_k.add(k)
                out.append(item)
        return out

    lives = merge_list("lives", key_fields=("url",))
    parses = merge_list("parses", key_fields=("name", "url"))

    flags = []
    for _src, cfg in results:
        for f in cfg.get("flags") or []:
            if f not in flags:
                flags.append(f)

    out = {
        "spider": spider_field,
        "wallpaper": main_cfg.get("wallpaper") or "",
        "sites": sites,
        "lives": lives,
        "parses": parses,
    }
    if flags:
        out["flags"] = flags
    for opt in ("doh", "ijk", "ads", "rules"):
        if main_cfg.get(opt):
            out[opt] = main_cfg[opt]

    path = os.path.join(ROOT, "tvbox.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    meta = {
        "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "main_source": main_src["name"],
        "sources_ok": [s["name"] for s, _ in results],
        "sources_failed": [s["name"] for s in srcs if s["name"] not in {x["name"] for x, _ in results}],
        "sites_before_probe": before,
        "sites_after_probe": len(sites),
        "per_source": stat,
        "probe_notes": notes,
        "spider": jar_info,
        "lives": len(lives),
        "parses": len(parses),
    }
    with open(os.path.join(ROOT, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    log("完成: tvbox.json + meta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
