#!/usr/bin/env python3
"""search · 词义：先翻自己的词库，再决定要不要上网。

优先级（这条顺序就是「省时间」的顺序）：
1. 本地词库（名词解释.md 的五层 + 名词表.md 的一行）→ 有就直接给，0 毫秒；
2. 已经查过的词 → 缓存 7 天，不再打扰网络；
3. 联网：百度百科 / 维基 / 萌娘 / MDN / StackOverflow / GitHub / npm / PyPI（并行，谁快谁先用）；
4. 中英对照：英文词查 dictionaryapi.dev，中文词走 MyMemory 免费翻译。

任何一个源挂了都只是少一条结果，不会让整个查询失败。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
TIMEOUT = 5
WEB_DEADLINE = 5.5
CACHE_TTL = 7 * 24 * 3600
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS term_cache (q TEXT PRIMARY KEY, payload TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS trans_cache (q TEXT PRIMARY KEY, payload TEXT, ts REAL);
"""

FIELD_PATTERNS = {
    "break": r"\*\*①\s*拆词\*\*[：:]\s*(.+?)(?=\n\s*\*\*②|\Z)",
    "one": r"\*\*②\s*一句话定义\*\*[：:]\s*(.+?)(?=\n\s*\*\*③|\Z)",
    "why": r"\*\*③\s*为什么需要它\*\*[：:]\s*(.+?)(?=\n\s*\*\*④|\Z)",
    "where": r"\*\*④\s*落在哪\*\*[：:]\s*(.+?)(?=\n\s*\*\*⑤|\n---|\Z)",
}


def _http_get(url: str, timeout: int = TIMEOUT) -> Optional[str]:
    request = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read(600_000).decode(charset, "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def _strip_html(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</p>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = (fragment.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#39;", "'"))
    return re.sub(r"[ \t\u00a0]+", " ", fragment).strip()


def _trim(text: str, limit: int = 400) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _plain(text: str) -> str:
    """去掉 Markdown 的强调符号，面板里不该出现 ** 和 `。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"^\s*[-*]\s+", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- 本地词库
def aliases_for(title: str) -> List[str]:
    raw = re.sub(r"^\d+[\.、]\s*", "", (title or "").strip())
    found = set()
    if raw:
        found.add(raw)
    match = re.match(r"^(.*?)[（(]([^）)]*)[)）]\s*$", raw)
    if match:
        cn, en = match.group(1).strip(), match.group(2).strip()
        for piece in (cn, en):
            found.add(piece)
            for part in re.split(r"\s*[/／]\s*", piece):
                if part.strip():
                    found.add(part.strip())
    for part in re.split(r"\s*[/／]\s*", raw):
        if part.strip():
            found.add(part.strip())
    return sorted({a.strip() for a in found if a and a.strip()}, key=len, reverse=True)


def parse_explanation(path: Path) -> List[Dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    marks = list(re.finditer(r"(?m)^##\s+", text))
    for position, mark in enumerate(marks):
        start = mark.end()
        end = marks[position + 1].start() if position + 1 < len(marks) else len(text)
        header = text[start:end].split("\n")[0].strip()
        body = text[start:end][len(header):]
        line = text.count("\n", 0, mark.start()) + 1
        title = re.sub(r"^\d+[\.、]\s*", "", header).strip()
        if not title:
            continue
        entry = {"title": title, "source": "%s#%s" % (path.name, header), "file": str(path),
                 "line": line, "aliases": aliases_for(title), "fields": {}, "table": {}}
        for key, pattern in FIELD_PATTERNS.items():
            match = re.search(pattern, body, re.S)
            if match:
                entry["fields"][key] = _trim(_plain(_strip_html(match.group(1))), 600)
        if entry["fields"]:
            entries.append(entry)
    return entries


def parse_term_table(path: Path) -> List[Dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.strip()
        if not line.startswith("|") or line.count("|") < 4:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name = cells[0]
        if not name or name in {"名词（中 / 英）", "名词"} or set(name) <= {"-", ":", " "}:
            continue
        if name.startswith("<") or "---" in name:
            continue
        entries.append({
            "title": name,
            "source": path.name,
            "file": str(path),
            "line": number,
            "aliases": aliases_for(name),
            "fields": {"one": _trim(_plain(cells[1]), 300), "where": _trim(_plain(cells[2]), 200)},
            "table": {"one": _trim(_plain(cells[1]), 300), "where": _trim(_plain(cells[2]), 200)},
        })
    return entries


class Glossary:
    """把名词解释.md 与名词表.md 合成一个可查的词库。"""

    def __init__(self, paths: List[Path]):
        self.paths = [Path(p) for p in paths]
        self.entries: List[Dict] = []
        self.index: Dict[str, Dict] = {}
        self.loaded_at = 0.0
        self.signature = ""
        self.lock = threading.RLock()
        self.reload(force=True)

    def _sig(self) -> str:
        parts = []
        for path in self.paths:
            try:
                stat = path.stat()
                parts.append("%s:%d:%d" % (path, stat.st_mtime, stat.st_size))
            except OSError:
                parts.append("%s:missing" % path)
        return "|".join(parts)

    def reload(self, force: bool = False) -> int:
        with self.lock:
            signature = self._sig()
            if not force and signature == self.signature:
                return len(self.entries)
            entries: List[Dict] = []
            for path in self.paths:
                if not path.exists():
                    continue
                if path.name.startswith("名词表"):
                    entries.extend(parse_term_table(path))
                else:
                    entries.extend(parse_explanation(path))
            # 合并：同名词条，五层正文优先，一句话取更完整的那条
            merged: Dict[str, Dict] = {}
            for entry in entries:
                key = self._norm(entry["title"])
                current = merged.get(key)
                if not current:
                    merged[key] = entry
                    continue
                for field, value in entry["fields"].items():
                    if value and (not current["fields"].get(field) or len(value) > len(current["fields"][field])):
                        current["fields"][field] = value
                current["aliases"] = sorted(set(current["aliases"]) | set(entry["aliases"]), key=len, reverse=True)
            self.entries = list(merged.values())
            index: Dict[str, Dict] = {}
            for entry in self.entries:
                for alias in entry["aliases"]:
                    key = self._norm(alias)
                    if not key:
                        continue
                    current = index.get(key)
                    # 同一个词被好几条认领时，留标题最短的那条（最像「正主」）
                    if current is None or len(self._norm(current["title"])) > len(self._norm(entry["title"])):
                        index[key] = entry
            self.index = index
            self.signature = signature
            self.loaded_at = time.time()
            return len(self.entries)

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[\s　·、，,。.：:；;\"'“”‘’（）()【】\[\]]+", "", (text or "").lower())

    def find(self, term: str) -> Optional[Dict]:
        self.reload()
        key = self._norm(term)
        if not key:
            return None
        if key in self.index:
            return self.index[key]
        # 包含关系：剪贴板常是一整句，或者带前后缀
        best = None
        best_len = 0
        for alias_key, entry in self.index.items():
            if len(alias_key) < 2:
                continue
            if alias_key in key and len(alias_key) > best_len:
                best, best_len = entry, len(alias_key)
        if best:
            return best
        for alias_key, entry in self.index.items():
            if len(key) >= 2 and key in alias_key and len(alias_key) - len(key) <= 3:
                return entry
        return None

    def suggest(self, term: str, limit: int = 6) -> List[Dict]:
        key = self._norm(term)
        if not key:
            return []
        out = []
        for alias_key, entry in self.index.items():
            if key in alias_key or alias_key in key:
                out.append({"title": entry["title"], "alias": alias_key})
            if len(out) >= limit:
                break
        return out


# ------------------------------------------------------------------ 联网源
def _baidu(term: str) -> Optional[Dict]:
    """百度百科的公开卡片接口。HTML 页面有反爬，这个接口能拿到摘要 + 中/外文名。"""
    api = ("https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"
           "?scope=103&format=json&appid=379020&bk_length=600&bk_key=" + urllib.parse.quote(term))
    raw = _http_get(api)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    summary = (data.get("abstract") or "").strip()
    if len(summary) < 10:
        summary = (data.get("desc") or "").strip()
    if len(summary) < 4:
        return None
    en = zh = ""
    for card in data.get("card") or []:
        name = card.get("name") or ""
        values = card.get("value") or []
        value = _strip_html(values[0]) if values else ""
        if not value:
            continue
        if name in {"外文名", "英文名", "英文名称"} and not en:
            en = value
        elif name in {"中文名", "中文名称"} and not zh:
            zh = value
        elif name in {"解 释", "解释", "定义", "含义"} and len(summary) < 20:
            summary = value
    return {"source": "百度百科", "title": data.get("title") or term,
            "url": "https://baike.baidu.com/item/" + urllib.parse.quote(term),
            "summary": _trim(summary, 460), "en": en, "zh": zh}


def _wikipedia(term: str) -> Optional[Dict]:
    api = "https://zh.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(term.replace(" ", "_"))
    raw = _http_get(api)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    extract = data.get("extract") or ""
    if not extract:
        return None
    return {"source": "维基百科", "title": data.get("title") or term,
            "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page")
                   or "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(term),
            "summary": _trim(extract, 420)}


def _moegirl(term: str) -> Optional[Dict]:
    api = ("https://zh.moegirl.org.cn/api.php?action=query&format=json&redirects=1"
           "&prop=extracts&exintro=1&explaintext=1&titles=" + urllib.parse.quote(term))
    raw = _http_get(api)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        pages = data["query"]["pages"]
    except (ValueError, KeyError, TypeError):
        return None
    for page in pages.values():
        extract = page.get("extract") or ""
        if extract:
            return {"source": "萌娘百科", "title": page.get("title") or term,
                    "url": "https://zh.moegirl.org.cn/" + urllib.parse.quote(page.get("title") or term),
                    "summary": _trim(extract, 360)}
    return None


def _mdn(term: str) -> Optional[Dict]:
    api = "https://developer.mozilla.org/api/v1/search?locale=zh-CN&q=" + urllib.parse.quote(term)
    raw = _http_get(api)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        docs = data.get("documents") or []
    except ValueError:
        return None
    if not docs:
        return None
    top = docs[0]
    summary = top.get("summary") or ""
    if not summary:
        return None
    return {"source": "MDN（前端文档）", "title": top.get("title") or term,
            "url": "https://developer.mozilla.org" + (top.get("mdn_url") or ""),
            "summary": _trim(summary, 300)}


def _stackoverflow(term: str) -> Optional[Dict]:
    api = ("https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance"
           "&site=stackoverflow&filter=default&q=" + urllib.parse.quote(term))
    raw = _http_get(api)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        items = data.get("items") or []
    except ValueError:
        return None
    if not items:
        return None
    top = items[0]
    return {"source": "Stack Overflow", "title": re.sub(r"<[^>]+>", "", top.get("title") or term),
            "url": top.get("link") or "",
            "summary": _trim("👍 %s · 回答 %s 条 · %s" % (
                top.get("score", 0), top.get("answer_count", 0),
                "已解决" if top.get("is_answered") else "未解决"), 200)}


def _en_dictionary(word: str) -> Optional[Dict]:
    if not re.match(r"^[A-Za-z][A-Za-z\-' ]{0,30}$", word.strip()):
        return None
    raw = _http_get("https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(word.strip()))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        first = data[0]
        meanings = first.get("meanings") or []
        parts = []
        for meaning in meanings[:2]:
            for definition in (meaning.get("definitions") or [])[:2]:
                parts.append("%s. %s" % (meaning.get("partOfSpeech", ""), definition.get("definition", "")))
        if not parts:
            return None
        phonetic = first.get("phonetic") or ""
        return {"source": "英语词典", "title": word.strip(), "url": "",
                "summary": _trim((phonetic + " " if phonetic else "") + " / ".join(parts), 320)}
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _translate(text: str, source: str = "en", target: str = "zh-CN") -> Optional[str]:
    query = urllib.parse.quote(text.strip()[:200])
    raw = _http_get("https://api.mymemory.translated.net/get?q=%s&langpair=%s|%s"
                    % (query, source, target))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        translated = (data.get("responseData") or {}).get("translatedText") or ""
    except ValueError:
        return None
    translated = translated.strip()
    if not translated or translated.lower() == text.strip().lower():
        return None
    return translated


# ---------------------------------------------------------------- DeepL
# 走 DeepL 网页版接口（www2.deepl.com/jsonrpc + LMT_handle_jobs），
# 和 Easydict 用的是同一条路：把 User-Agent 写成 DeepL 官方 Mac 客户端的格式。
# 这条路不需要账号、不用 key，代价是「同一个 IP 问快了就 429」，
# 所以这里排队 + 熔断：一次只放一个出去，撞到 429 就歇一分半。
DEEPL_GAP = 6.0        # 两次请求之间至少隔这么久（实测 3 秒就会被 429）
DEEPL_BREAK = 120.0    # 撞 429 之后歇这么久，别把 IP 打黑
_deepl_lock = threading.Lock()
_deepl_next = [0.0]
_deepl_seq = [1000]
_deepl_memo: Dict[str, tuple] = {}      # 同一个词只问一次
_deepl_error = [0.0]   # 最近一次「接口报错」的时间，_deepl() 靠它判断要不要出报错卡

# 接口限流 / 版本检查时，DeepL 会把一整段英文提示当成译文塞回来，
# 例如 "You're using an older version of DeepL. To continue using the app..."。
# 这种文本一律按报错处理，不当翻译结果展示。
DEEPL_ERROR_PAT = re.compile(
    r"older version of deepl|to continue using the app|deepl pro|"
    r"rate limit|too many requests|service unavailable", re.I)


def _deepl_is_error(source: str, translated: str) -> bool:
    """判断「译文」其实是不是一段报错提示。只对短输入做膨胀判定，避免误伤长句翻译。"""
    if not translated:
        return False
    if DEEPL_ERROR_PAT.search(translated):
        return True
    # 一个短词翻出一大段：正常翻译不会膨胀成这样
    return len(source) <= 24 and len(translated) > max(60, len(source) * 5)


def _clean_cached(item: Dict) -> Dict:
    """缓存里可能躺着以前的 DeepL 报错页（缓存 7 天，光靠过期不行）。
    取出来时换成一张只有标志、没有原文的报错卡。"""
    if item.get("error") or (item.get("source") == "DeepL"
                            and _deepl_is_error(item.get("title") or "",
                                                item.get("summary") or "")):
        return {"source": item.get("source") or "DeepL",
                "title": item.get("title") or "", "url": "",
                "summary": "", "zh": "", "en": "", "error": "接口异常，已跳过"}
    return item


def deepl_translate(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text or len(text) > 500:
        return None
    memo = _deepl_memo.get(text)
    if memo and time.time() - memo[1] < 1800:
        return memo[0]

    target = "EN" if re.search(r"[\u4e00-\u9fff]", text) else "ZH"
    deadline = time.time() + 5.0
    while True:                                    # 排队：轮到自己才发
        with _deepl_lock:
            now = time.time()
            if now >= _deepl_next[0]:
                _deepl_next[0] = now + DEEPL_GAP
                _deepl_seq[0] += 1
                seq = _deepl_seq[0]
                break
            wait = min(_deepl_next[0] - now, 0.15)
        if time.time() > deadline:
            return None
        time.sleep(wait)

    rid = int(time.time() * 1000) % 100000 + seq
    payload = {
        "jsonrpc": "2.0",
        "method": "LMT_handle_jobs",
        "params": {
            "jobs": [{"kind": "default", "raw_en_sentence": text,
                      "raw_en_context_before": [], "raw_en_context_after": [],
                      "preferred_num_beams": 2}],
            "lang": {"user_preferred_langs": ["ZH", "EN"] if target == "ZH" else ["EN", "ZH"],
                     "source_lang_user_selected": "auto", "target_lang": target},
            "priority": -1,
            # 这个只对「译成中文」有效，译成英文时带上会直接被拒（400）
            "commonJobParams": {"regionalVariant": "zh-Hans"} if target == "ZH" else {},
            "timestamp": int(time.time() * 1000),
        },
        "id": rid,
    }
    body = json.dumps(payload, ensure_ascii=False)
    # DeepL 会抽查 jsonrpc 这行的空格格式，两种写法按 id 轮换，跟官方客户端一致
    spaced = '"method" : "' if ((seq + 3) % 13 == 0 or (seq + 5) % 29 == 0) else '"method": "'
    body = body.replace('"method":"', spaced, 1)

    request = urllib.request.Request(
        "https://www2.deepl.com/jsonrpc",
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "DeepL/16273800 CFNetwork/1494.0.7 Darwin/23.4.0",
                 "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        if error.code == 429:                      # 被限流了，歇一会
            with _deepl_lock:
                _deepl_next[0] = time.time() + DEEPL_BREAK
        return None
    except Exception:
        return None

    beams = ((data.get("result") or {}).get("translations") or [{}])[0].get("beams") or []
    if not beams:
        return None
    translated = (beams[0].get("postprocessed_sentence") or "").strip()
    if not translated or translated.lower() == text.lower():
        return None
    if _deepl_is_error(text, translated):
        _deepl_error[0] = time.time()
        return None
    _deepl_memo[text] = (translated, time.time())
    if len(_deepl_memo) > 400:
        oldest = sorted(_deepl_memo.items(), key=lambda kv: kv[1][1])[:120]
        for key, _ in oldest:
            _deepl_memo.pop(key, None)
    return translated


def _deepl(term: str) -> Optional[Dict]:
    """DeepL 单独出一张卡片：它翻得最顺，值得单列。
    报错时只回一张「接口异常」标志卡，不带报错原文。"""
    started = time.time()
    translated = deepl_translate(term)
    if not translated:
        if _deepl_error[0] >= started:
            return {"source": "DeepL", "title": term, "url": "",
                    "summary": "", "zh": "", "en": "", "error": "接口异常，已跳过"}
        return None
    has_cn = bool(re.search(r"[\u4e00-\u9fff]", term))
    return {"source": "DeepL",
            "title": term,
            "url": "https://www.deepl.com/translator",
            "summary": translated,
            "zh": "" if has_cn else translated,
            "en": translated if has_cn else ""}


def _github(term: str) -> Optional[Dict]:
    """GitHub 仓库搜索（官方 REST，免 key）。带库名/技术名去查，能直接看到这是什么东西、多少人用。
    不传 sort，用 GitHub 自己的相关度排序——按星排会把 freeCodeCamp 这种巨型仓库顶上来。"""
    api = ("https://api.github.com/search/repositories?per_page=1&q="
           + urllib.parse.quote(term))
    raw = _http_get(api)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        items = data.get("items") or []
    except ValueError:
        return None
    if not items:
        return None
    top = items[0]
    desc = _trim((top.get("description") or "").strip(), 180)
    lang = top.get("language") or ""
    stars = top.get("stargazers_count") or 0
    summary = "⭐ %s 星 · %s%s" % (stars, (lang + " · ") if lang else "",
                                  desc or "（仓库没写简介）")
    return {"source": "GitHub",
            "title": top.get("full_name") or term,
            "url": top.get("html_url") or "",
            "summary": _trim(summary, 300)}


def _npm(term: str) -> Optional[Dict]:
    """npm 官方搜索接口：查前端/Node 包。只取第一条，避免整页都是同名包。"""
    api = ("https://registry.npmjs.org/-/v1/search?size=1&text="
           + urllib.parse.quote(term))
    raw = _http_get(api)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        objects = data.get("objects") or []
    except ValueError:
        return None
    if not objects:
        return None
    top = objects[0]
    package = top.get("package") or {}
    name = package.get("name") or ""
    if not name:
        return None
    score = (top.get("score") or {}).get("final") or 0
    summary = "v%s · 热度 %.2f · %s" % (package.get("version") or "?",
                                       score,
                                       _trim(package.get("description") or "（没写简介）", 160))
    return {"source": "npm（前端包）", "title": name,
            "url": ((package.get("links") or {}).get("npm")
                    or "https://www.npmjs.com/package/" + urllib.parse.quote(name)),
            "summary": _trim(summary, 280)}


def _pypi(term: str) -> Optional[Dict]:
    """PyPI 官方 JSON 接口。它只认包名精确匹配，词不是包名时静默返回空，不浪费一张卡。"""
    name = term.strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$", name):
        return None
    raw = _http_get("https://pypi.org/pypi/" + urllib.parse.quote(name) + "/json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        info = data.get("info") or {}
    except ValueError:
        return None
    summary = _trim((info.get("summary") or "").strip(), 170)
    if not summary:
        return None
    return {"source": "PyPI（Python 包）",
            "title": info.get("name") or name,
            "url": info.get("package_url") or ("https://pypi.org/project/" + name + "/"),
            "summary": _trim("v%s · %s" % (info.get("version") or "?", summary), 280)}


# ---------------------------------------------------------------- 查梗（娱乐网站）
# 站点目录（meme_sites.json）里所有「带 api 字段」的站，按顺序试，谁先查到用谁。
# 解析器按接口地址的特征分派，不写死站名；日后任何站填上 api 字段即自动生效：
#   api.php?action=query  → MediaWiki（query.pages[].extract）
#   wp-json/wp/v2/posts   → WordPress REST（数组，每条 title / link / excerpt）
# 2026-11-09 实测：萌娘百科 api.php 间歇可达（连测 3 次 1 次 200），墨鱼词典 WP REST 稳定；
# 多站遍历正好互补，某站挂了自动试下一个；全挂返回 None → 前端降级到目录点开。
def _meme_mediawiki(site: Dict, data, term: str) -> Optional[Dict]:
    query = data.get("query") if isinstance(data, dict) else None
    pages = query.get("pages", {}) if isinstance(query, dict) else {}
    if not isinstance(pages, dict):
        return None
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        extract = (page.get("extract") or "").strip()
        # MediaWiki 查无结果时返回的页面 extract 为空，或 missing 页面
        if not extract or extract.lower().startswith(("undefined", "not found", "并不存在", "页面不存在")):
            continue
        title = page.get("title") or term
        base = (site.get("url") or "").rstrip("/")
        return {
            "source": site.get("name") or "梗站",
            "title": title,
            "url": (base + "/" + urllib.parse.quote(title.replace(" ", "_"))) if base else "",
            "summary": _trim(extract, 480),
        }
    return None


def _meme_wordpress(site: Dict, data, term: str) -> Optional[Dict]:
    if not isinstance(data, list):
        return None
    # WordPress REST 的 search 是全文模糊匹配，会把「只是在正文里提到这个词」的
    # 不相干词条排到前面（实测查「破防」第一条是「雀润」）。只认标题包含查询词的
    # 结果；一条都没有就返回 None 交给下一个站 / 目录，不硬凑一个错的答案。
    for post in data:
        if not isinstance(post, dict):
            continue
        title_obj = post.get("title") if isinstance(post.get("title"), dict) else {}
        excerpt_obj = post.get("excerpt") if isinstance(post.get("excerpt"), dict) else {}
        title = _strip_html(title_obj.get("rendered") or "")
        summary = _trim(_strip_html(excerpt_obj.get("rendered") or ""), 480)
        if not title or not summary or term not in title:
            continue
        return {
            "source": site.get("name") or "梗站",
            "title": title,
            "url": post.get("link") or "",
            "summary": summary,
        }
    return None


def _meme_primary_api(term: str) -> Optional[Dict]:
    for site in load_meme_sites():
        if not isinstance(site, dict):
            continue
        api = site.get("api")
        if not isinstance(api, str) or not api.strip():
            continue
        raw = _http_get(api.replace("{q}", urllib.parse.quote(term)), timeout=4)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if "wp-json" in api or "/posts" in api:
            hit = _meme_wordpress(site, data, term)
        else:
            hit = _meme_mediawiki(site, data, term)
        if hit:
            return hit
    return None


# 站点目录：server/meme_sites.json（可在面板里增删；不在代码里写死）。
# 这份是 xuhbot/src/data/meme-sites.json 的同步副本，由「同步梗站清单.command」生成。
MEME_SITES_PATH = Path(__file__).resolve().parent / "meme_sites.json"


def load_meme_sites() -> List[Dict]:
    try:
        return json.loads(MEME_SITES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def meme_lookup(term: str) -> Optional[Dict]:
    """查一个梗：遍历站点目录里所有带 api 的站；查不到/全挂返回 None，上层降级到目录。"""
    return _meme_primary_api(term)


# 源名固定，前端顶栏小标、配置里的开关都用这个名字对号
WEB_SOURCES = [
    ("百度百科", _baidu),
    ("维基百科", _wikipedia),
    ("萌娘百科", _moegirl),
    ("MDN（前端文档）", _mdn),
    ("Stack Overflow", _stackoverflow),
    ("GitHub", _github),
    ("npm（前端包）", _npm),
    ("PyPI（Python 包）", _pypi),
    ("DeepL", _deepl),
]
SOURCE_ORDER = [name for name, _ in WEB_SOURCES]

# 「娱乐」分类总闸管哪些联网源 —— 单点定义，后端过滤和前端小标/三态都读它，
# 不再在 JS 里另写一份名单。查梗面板 / 梗站目录也归这个总闸（见 server.py）。
FUN_SOURCES = ("萌娘百科",)

class Dict_:
    def __init__(self, db_path: Path, glossary_paths: List[Path]):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(CACHE_SCHEMA)
        self.conn.commit()
        self.glossary = Glossary(glossary_paths)
        self.pool = ThreadPoolExecutor(max_workers=10)

    # -- 缓存 -------------------------------------------------------------
    def _cache_get(self, table: str, key: str) -> Optional[Dict]:
        with self.lock:
            row = self.conn.execute("SELECT payload, ts FROM %s WHERE q=?" % table, (key,)).fetchone()
        if not row or time.time() - row["ts"] > CACHE_TTL:
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

    def _cache_set(self, table: str, key: str, payload) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO %s(q, payload, ts) VALUES(?,?,?) "
                              "ON CONFLICT(q) DO UPDATE SET payload=excluded.payload, ts=excluded.ts"
                              % table, (key, json.dumps(payload, ensure_ascii=False), time.time()))
            self.conn.commit()

    # -- 联网 -------------------------------------------------------------
    def web_lookup(self, term: str, allow_network: bool = True,
                   sources: Optional[List[str]] = None) -> List[Dict]:
        """抓网页摘要。sources 为 None 表示全都问；否则只问名单里的源。

        缓存里存的永远是「所有源的结果」，过滤放在返回时做——
        这样关掉再打开某个源，不用重新联网。
        """
        if not allow_network:
            return []
        wanted = set(sources) if sources is not None else set(SOURCE_ORDER)
        if not wanted:
            return []
        key = "web:" + term.lower()
        cached = self._cache_get("term_cache", key)
        if cached is None:
            cached = self._gather_web(term)
            if cached:
                self._cache_set("term_cache", key, cached)
        # 按「谁先查回来谁在前」排：她要看的是先出结果的那张卡。
        # 老缓存里可能存着 DeepL 的报错页，取出来时一并剔掉（缓存有 7 天，不能只靠过期）。
        picked = [_clean_cached(item) for item in cached if item.get("source") in wanted]
        picked.sort(key=lambda item: item.get("seq", 0))
        return picked

    # -- 逐条抓：每回来一个源就回调一次，前端能一张张卡片划进来 ------------
    def web_stream(self, term: str, on_item=None,
                   sources: Optional[List[str]] = None) -> List[Dict]:
        """和 web_lookup 一样的抓取，但每抓到一条立刻 on_item(item)。

        返回最终列表（同样按到达先后排）。缓存照旧，重查不再联网。
        """
        wanted = set(sources) if sources is not None else set(SOURCE_ORDER)
        key = "web:" + term.lower()
        cached = self._cache_get("term_cache", key)
        if cached is not None:
            picked = [_clean_cached(item) for item in cached if item.get("source") in wanted]
            picked.sort(key=lambda item: item.get("seq", 0))
            for item in picked:
                if on_item:
                    on_item(item)
            return picked

        collected: List[Dict] = []

        def gather():
            seq = 0
            futures = {self.pool.submit(func, term): name
                       for name, func in WEB_SOURCES if name in wanted}
            try:
                for future in as_completed(futures, timeout=WEB_DEADLINE):
                    try:
                        item = future.result()
                    except Exception:
                        item = None
                    if not (item and (item.get("summary") or item.get("error"))):
                        continue
                    if item.get("source") not in wanted:
                        continue
                    if item.get("error"):
                        # 报错卡只推给前端当标志，不进缓存、不留档
                        if on_item:
                            try:
                                on_item(item)
                            except Exception:
                                pass
                        continue
                    item["seq"] = seq
                    seq += 1
                    collected.append(item)
                    if on_item:
                        try:
                            on_item(item)
                        except Exception:
                            pass
            except Exception:
                pass
            for future in futures:
                future.cancel()

        gather()
        if collected:
            collected.sort(key=lambda item: item.get("seq", 0))
            self._cache_set("term_cache", key, collected)
        return collected

    def _gather_web(self, term: str) -> List[Dict]:
        """老实等全部源回来（web_lookup 的老路径，给不带流的地方用）。"""
        results: List[Dict] = []
        futures = {self.pool.submit(func, term): name for name, func in WEB_SOURCES}
        try:
            for future in as_completed(futures, timeout=WEB_DEADLINE):
                try:
                    item = future.result()
                except Exception:
                    item = None
                if item and (item.get("summary") or item.get("error")):
                    results.append(item)
        except Exception:
            pass
        for future in futures:
            future.cancel()
        results.sort(key=lambda item: item.get("seq", 0))
        for index, item in enumerate(results):
            item.setdefault("seq", index)
        return results

    def translate(self, term: str, allow_network: bool = True,
                  deepl: bool = True) -> Optional[Dict]:
        text = (term or "").strip()
        if not text or not allow_network:
            return None
        key = "trans:" + text.lower()
        cached = self._cache_get("trans_cache", key)
        if cached is not None:
            return cached
        is_english = bool(re.match(r"^[A-Za-z][A-Za-z\-\s']{0,40}$", text))
        is_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))
        payload = {"en": "", "zh": ""}
        if is_english and not is_chinese:
            payload["en"] = text
            payload["zh"] = (deepl_translate(text) if deepl else None) \
                or _translate(text, "en", "zh-CN") or ""
        elif is_chinese:
            payload["zh"] = text
            payload["en"] = (deepl_translate(text) if deepl else None) \
                or _translate(text, "zh-CN", "en") or ""
        else:
            payload["en"] = text
        if not payload["en"] and not payload["zh"]:
            return None
        self._cache_set("trans_cache", key, payload)
        return payload

    # -- 汇总 -------------------------------------------------------------
    def resolve(self, term: str, allow_network: bool = True, web: bool = True) -> Dict:
        term = (term or "").strip()
        entry = self.glossary.find(term) if term else None
        local = None
        if entry:
            fields = entry.get("fields") or {}
            local = {
                "title": entry["title"],
                "one": fields.get("one", ""),
                "break": fields.get("break", ""),
                "why": fields.get("why", ""),
                "where": fields.get("where", ""),
                "source": entry.get("source", ""),
                "file": entry.get("file", ""),
                "line": entry.get("line", 0),
                "aliases": entry.get("aliases", [])[:6],
            }
        payload = {
            "term": term,
            "kind": self._kind(term),
            "local": local,
            "web": [],
            "trans": None,
            "suggest": [],
            "fetchedAt": time.time(),
        }
        if not term:
            return payload
        if web and allow_network:
            payload["web"] = self.web_lookup(term, allow_network)
            payload["trans"] = self._merge_trans(term, payload["kind"], payload["web"],
                                                 self.translate(term, allow_network))
        if not local:
            payload["suggest"] = self.glossary.suggest(term)
        return payload

    def _merge_trans(self, term: str, kind: str, web: List[Dict],
                     trans: Optional[Dict]) -> Optional[Dict]:
        """中英对照的优先级：自己的词库 → 百度百科的「中文名/外文名」→ 机器翻译。

        机器翻译放最后，是因为它最容易出错（比如把「缓存」翻成 Redis）。
        """
        en = ""
        zh = ""
        entry = self.glossary.find(term)
        if entry:
            for alias in entry.get("aliases", []):
                if re.match(r"^[A-Za-z][A-Za-z\-\s]{0,30}$", alias) and not en:
                    en = alias
                if re.search(r"[\u4e00-\u9fff]", alias) and not zh:
                    # 「缓存（cache）」只取中文那半边，括号里的英文已经单独显示了
                    zh = re.sub(r"[（(][^）)]*[)）]", "", alias).strip() or alias
        for item in web or []:
            item_en = (item.get("en") or "").strip()
            item_zh = (item.get("zh") or "").strip()
            if kind == "cn":
                if item_en and not en and item_en.lower() != term.lower():
                    en = item_en
                if not zh:
                    zh = term
            elif kind in {"en", "phrase"}:
                if item_zh and not zh and item_zh.lower() != term.lower() \
                        and not re.match(r"^[A-Za-z\s]+$", item_zh):
                    zh = item_zh
        if not en:
            en = (trans or {}).get("en") or ""
        if not zh:
            zh = (trans or {}).get("zh") or ""
        if not en and not zh:
            return None
        return {"en": en, "zh": zh}

    @staticmethod
    def _kind(term: str) -> str:
        has_cn = bool(re.search(r"[\u4e00-\u9fff]", term))
        has_en = bool(re.search(r"[A-Za-z]", term))
        if has_cn and has_en:
            return "mixed"
        if has_cn:
            return "cn"
        if has_en:
            return "en" if " " not in term.strip() else "phrase"
        return "other"

    # -- 查梗（娱乐网站）---------------------------------------------------
    def meme_lookup(self, term: str) -> Optional[Dict]:
        """查一个梗：走 cnmeme 公开 API；查不到/站点挂了返回 None，上层降级。"""
        return meme_lookup(term)

    def meme_sites(self) -> List[Dict]:
        """娱乐/梗站点目录（sever/meme_sites.json 的内容）。"""
        return load_meme_sites()

    def stats(self) -> Dict:
        self.glossary.reload()
        return {"entries": len(self.glossary.entries), "aliases": len(self.glossary.index),
                "loadedAt": self.glossary.loaded_at}
