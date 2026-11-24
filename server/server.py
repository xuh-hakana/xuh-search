#!/usr/bin/env python3
"""search · 本地服务（只读文件 + 只监听 127.0.0.1）。

接口一览
  GET  /api/health          活着没
  GET  /api/state           全部状态：登记目录、开关、索引进度、变更、剪贴板
  GET  /api/clip            现在剪贴板是什么
  GET  /api/lookup?q=       查一个词：本地词库 + 本地命中 + 联网
  GET  /api/search?q=&scope=roots|disk   搜登记目录 / 搜整台电脑
  GET  /api/changes         已变更 / 已缺失
  GET  /api/events          SSE，服务器有新情况就推
  POST /api/roots           增删登记目录（action=add|remove|reorder）
  POST /api/switch          拨开关
  POST /api/reindex         重建索引
  POST /api/restart         拉起一个新的自己接管（同端口、同数据目录），旧的退场
  POST /api/ack             把这些变更标记成「看过了」
  POST /api/open            打开文件 / 在访达里显示 / 跳到行
  GET  /api/pick-folder     弹系统选文件夹
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

import extract
from dict import Dict_, FUN_SOURCES
from indexdb import Index
from store import Store

APP_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = APP_ROOT / "web"
VERSION = "0.1.0"

# 重启时记下的启动参数：用来拉起一个“同端口、同数据目录”的自己
RESTART_ARGV = None
SERVER_PORT = None

# 长句拆词时用来滤掉纯虚词的二字组合（「的句」「是我」这种），
# 不滤的话长句能拆出一堆没信息量的片段，把真正相关的文件挤下去。
STOP_CHARS = set("的了是在和与或及等之其而将已就都也还很太挺更最又再只才"
                 "个把被给对中上下里外这那我你他她它请帮想要会能可做说看"
                 "知道些一点什么怎样哪")


def reap_stale(data_dir: Path) -> None:
    """上一次没退干净的自己先送走。

    否则两个服务开着同一个索引库：一个在写、一个在读，界面随便点一下都能卡住。
    只杀命令行里确实是 search/server/server.py 的进程，别的绝不碰。
    """
    runtime = Path(data_dir) / "runtime.json"
    try:
        info = json.loads(runtime.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    pid = int(info.get("pid") or 0)
    if not pid or pid == os.getpid():
        return
    try:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return
    if "search/server/server.py" not in command:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    print("[search] 清理了上次残留的服务 %d" % pid, flush=True)
MAX_BODY = 1_000_000


def spawn_successor(port: int, data_dir: Path) -> bool:
    """拉起一个新的自己（同端口、同数据目录、不弹浏览器），由它接管。

    新进程起来后会用 reap_stale 把旧的那具送走，再在同样的 127.0.0.1:port 上监听——
    前端 WKWebView 指向的就是这个固定端口，所以必须复用，不能换。
    """
    import subprocess
    script = Path(__file__).resolve()
    base = list(RESTART_ARGV) if RESTART_ARGV else []
    # --port / --data-dir / --no-open 这几个会被强制重写，先把旧的清掉
    cleaned, i = [], 0
    while i < len(base):
        a = base[i]
        if a in ("--port", "--data-dir"):
            i += 2
            continue
        if a == "--no-open":
            i += 1
            continue
        if a.startswith("--port=") or a.startswith("--data-dir="):
            i += 1
            continue
        cleaned.append(a)
        i += 1
    args = ["--port", str(port), "--no-open", "--data-dir", str(data_dir), *cleaned]
    try:
        subprocess.Popen([sys.executable, str(script), *args],
                         cwd=str(script.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
SKIP_MDFIND = ("/System/", "/private/", "/Library/", "/usr/", "/Applications/",
               "/Volumes/", "/.Spotlight-V100", "/dev/")
PROJECT_ROOT = Path.home() / "Documents" / "xuhuan mac"


# --------------------------------------------------------------------- 服务
class SearchService:
    def __init__(self, store: Store):
        self.store = store
        self.index = Index(store.data_dir / "index.sqlite3")
        glossary_paths = [
            PROJECT_ROOT / "名词解释" / "名词解释.md",
            PROJECT_ROOT / "名词表" / "名词表.md",
            PROJECT_ROOT / "xuhbot" / "persona" / "3-系统提示词-固定段.md",
            PROJECT_ROOT / "xuhbot" / "persona" / "4-系统提示词-动态段.md",
            PROJECT_ROOT / "xuhbot" / "persona" / "5-公共规则层.md",
        ]
        self.dict = Dict_(store.data_dir / "terms.sqlite3", glossary_paths)
        self.clip_text = ""
        self.clip_at = 0.0
        self.clip_seq = 0
        self.subscribers: List[queue.Queue] = []
        self.sub_lock = threading.Lock()
        self.build_thread: Optional[threading.Thread] = None
        self.index_abort = threading.Event()   # 【管理】一关就把正在跑的扫描叫停
        self.stop_flag = False
        # 命令方块：另开一个索引库，互不干扰
        self.cb_index = Index(store.data_dir / "commandblock.sqlite3")
        try:  # 建索引时别把读请求卡死
            self.cb_index.conn.execute("PRAGMA busy_timeout=300")
        except Exception:
            pass
        self.cb_build: Optional[threading.Thread] = None
        self.cb_built_root = ""
        self._cb_cache = {"at": 0.0, "info": {}}
        # 联网抓取任务：一个词一份，谁先查回来谁先进 items，前端一张张取走
        self.web_jobs: Dict[str, Dict] = {}
        self.web_lock = threading.Lock()

    # -- 事件推送 ---------------------------------------------------------
    def publish(self, event: Dict) -> None:
        with self.sub_lock:
            dead = []
            for box in self.subscribers:
                try:
                    box.put_nowait(event)
                except queue.Full:
                    dead.append(box)
            for box in dead:
                self.subscribers.remove(box)

    def subscribe(self) -> queue.Queue:
        box: queue.Queue = queue.Queue(maxsize=64)
        with self.sub_lock:
            self.subscribers.append(box)
        return box

    def unsubscribe(self, box: queue.Queue) -> None:
        with self.sub_lock:
            if box in self.subscribers:
                self.subscribers.remove(box)

    # -- 剪贴板 -----------------------------------------------------------
    def clipboard_loop(self) -> None:
        """0.7 秒看一眼剪贴板。变了才推事件，没变什么都不做。"""
        last = ""
        while not self.stop_flag:
            try:
                result = subprocess.run(["pbpaste"], capture_output=True, timeout=3)
                text = result.stdout.decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                text = ""
            text = text.strip()
            if text and len(text) > 4000:
                text = text[:4000]
            if text and text != last:
                last = text
                self.clip_text = text
                self.clip_at = time.time()
                self.clip_seq += 1
                self.publish({"type": "clip", "text": text, "seq": self.clip_seq,
                              "term": self.term_of(text), "at": self.clip_at})
            elif not text:
                last = ""
            time.sleep(0.7)

    @staticmethod
    def term_of(text: str) -> str:
        """从剪贴板里挑出「要查的那个词」。"""
        first = (text or "").strip().split("\n")[0].strip()
        first = re.sub(r"^[#>*\-\s\"'“”「」【】]+", "", first)
        first = re.sub(r"[，。；：、!?！？\s]+$", "", first)
        if len(first) <= 40:
            return first
        # 长句：优先挑括号里的词，其次挑最长的英文单词 / 连续中文
        match = re.search(r"[（(]([^）)]{2,30})[)）]", first)
        if match:
            return match.group(1).strip()
        english = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,30}", first)
        if english:
            return max(english, key=len)
        chinese = re.findall(r"[\u4e00-\u9fff]{2,10}", first)
        if chinese:
            return max(chinese, key=len)
        return first[:40]

    # -- 索引 -------------------------------------------------------------
    def ensure_index(self, force: bool = False) -> None:
        # 【管理】关掉 = 索引服务停掉：不扫、不建、不报变更
        if not self.store.switches().get("manage", True):
            self.index_abort.set()
            return
        self.index_abort.clear()
        roots = self.store.root_paths()
        if not roots:
            return
        if self.build_thread and self.build_thread.is_alive():
            return
        stats = self.index.stats()
        if not force and stats["files"] > 0 and time.time() - stats["lastBuildAt"] < 120:
            return

        def run() -> None:
            self.publish({"type": "index", "state": "start"})
            try:
                result = self.index.rebuild(roots, on_progress=lambda p: self.publish(
                    {"type": "index", "state": "progress", "progress": p}),
                    should_stop=self.index_abort.is_set)
                self.publish({"type": "index", "state": "done", "result": result,
                              "stats": self.index.stats()})
            except Exception as error:  # 索引失败不该带走整个服务
                self.publish({"type": "index", "state": "error", "message": str(error)})

        self.build_thread = threading.Thread(target=run, daemon=True)
        self.build_thread.start()

    # -- 状态 -------------------------------------------------------------
    def state(self) -> Dict:
        switches = self.store.switches()
        changes = self.index.changes(self.store.acked()) if switches.get("manage", True) else []
        return {
            "version": VERSION,
            "roots": self.store.roots(),
            "switches": switches,
            "sources": self.store.sources(),
            # 分类名单单点下发：前端小标 / 三态 / 一键全开都读它，不在 JS 里另写一份
            "groups": {"web": [n for n in self.store.sources() if n not in FUN_SOURCES],
                       "fun": list(FUN_SOURCES)},
            "scope": self.store.effective_scope(),
            "index": self.index.stats(),
            "changes": changes,
            "dict": self.dict.stats(),
            "clip": {"text": self.clip_text, "term": self.term_of(self.clip_text),
                     "at": self.clip_at, "seq": self.clip_seq},
            "commands": self.command_block_status(),
            "projectRoot": str(PROJECT_ROOT),
        }

    # -- 命令方块 ---------------------------------------------------------
    def command_block_status(self) -> Dict:
        """看命令方块在不在跑、它现在开着哪个文件夹、里面有哪些新文件。只读，不改它。"""
        now = time.time()
        cached = self._cb_cache
        if cached["info"] and now - cached["at"] < 20:
            return cached["info"]
        info = {"running": False, "port": 0, "root": "", "rootName": "",
                "newFiles": [], "appExists": False}
        info["appExists"] = (PROJECT_ROOT / "命令方块" / "命令方块.app").exists()
        port = self._find_command_block_port()
        if port:
            info["running"] = True
            info["port"] = port
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/tree" % port, timeout=4) as response:
                    data = json.loads(response.read().decode("utf-8", "replace"))
                info["root"] = data.get("root") or ""
                info["rootName"] = data.get("rootName") or ""
            except Exception:
                pass
        info["newFiles"] = self.cb_new_files(info["root"]) if info["root"] else []
        cached["at"], cached["info"] = now, info
        return info

    # -- 命令方块的文件（借用同一套抽取器，单独一个库）--------------------
    def _cb_ensure(self, root: str) -> None:
        if not root or not Path(root).is_dir():
            return
        stats = self.cb_index.stats()
        if self.cb_build and self.cb_build.is_alive():
            return
        if self.cb_built_root == root and stats["files"] and time.time() - stats["lastBuildAt"] < 300:
            return

        def run() -> None:
            try:
                self.cb_index.rebuild([root])
                self.cb_built_root = root
                self.publish({"type": "cb", "state": "ready", "stats": self.cb_index.stats()})
            except Exception as error:
                self.publish({"type": "cb", "state": "error", "message": str(error)})

        self.cb_build = threading.Thread(target=run, daemon=True)
        self.cb_build.start()

    def cb_hits(self, terms: List[str], limit: int = 16) -> List[Dict]:
        root = self.command_block_status().get("root") or ""
        if not root:
            return []
        self._cb_ensure(root)
        hits = self.cb_index.search(terms, limit=limit)
        for hit in hits:
            hit["root"] = "commandblock"
            hit["dir"] = os.path.dirname(hit["path"])
        return hits

    def cb_new_files(self, root: str = "") -> List[Dict]:
        """命令方块里「这次新出现、她还没在这边点开过」的文件。"""
        noise_ext = {".log", ".jsonl", ".lock", ".tmp", ".pid", ".pyc", ".sqlite3", ".db"}
        try:
            # 命令方块那个库也在后台建，读的时候一样要排队
            with self.cb_index.lock:
                rows = self.cb_index.conn.execute("SELECT path, mtime FROM docs").fetchall()
        except Exception:
            return []
        current = {row["path"]: row["mtime"] for row in rows}
        known = set(self.store.cb_known())
        if not known:                       # 第一次：把现在的一批当基线，不算新增
            self.store.set_cb_known(list(current))
            return []
        out: List[Dict] = []
        for path, mtime in current.items():
            if path in known:
                continue
            if path.startswith(str(PROJECT_ROOT / "search") + os.sep) or "/node_modules/" in path:
                continue
            if os.path.splitext(path)[1].lower() in noise_ext or os.path.basename(path).startswith("."):
                continue
            out.append({"path": path, "name": os.path.basename(path), "mtime": mtime})
        out.sort(key=lambda item: item["mtime"], reverse=True)
        return out[:40]

    def cb_ack_new(self) -> None:
        try:
            with self.cb_index.lock:
                rows = self.cb_index.conn.execute("SELECT path FROM docs").fetchall()
        except Exception:
            rows = []
        self.store.set_cb_known([row["path"] for row in rows])
        self.store.set_cb_seen_at(time.time())

    def warm_command_block(self) -> None:
        """启动后先把命令方块的文件收进索引（它没开就什么都不做）。"""
        status = self.command_block_status()
        if status.get("running") and status.get("root"):
            if self.store.cb_seen_at() <= 0:      # 第一次跑：从此刻起算「新增」
                self.store.set_cb_seen_at(time.time())
            self._cb_ensure(status["root"])

    @staticmethod
    def _find_command_block_port() -> int:
        try:
            result = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, timeout=4)
        except (OSError, subprocess.SubprocessError):
            return 0
        for line in result.stdout.decode("utf-8", "replace").split("\n"):
            if "命令方块/server/server.py" not in line:
                continue
            match = re.search(r"--port\s+(\d+)", line)
            if match:
                return int(match.group(1))
        return 0

    # -- 查询 -------------------------------------------------------------
    def lookup(self, term: str, use_web: bool = False) -> Dict:
        """快路径：本地词库 + 本地命中，几十毫秒出结果。"""
        switches = self.store.switches()
        wants_web = use_web and switches.get("web", True)
        wants_files = switches.get("manage", True)
        payload = self.dict.resolve(term, allow_network=wants_web, web=wants_web)
        terms = self._clip_terms(term)
        payload["terms"] = terms
        # 接了命令方块，文件命中就换成它里面的
        cb = self.command_block_status() if (wants_files and switches.get("docked")) else None
        if cb and cb.get("running") and cb.get("root"):
            payload["hits"] = self.cb_hits(terms)
            payload["hitsSource"] = "commandblock"
            payload["hitsRoot"] = cb.get("rootName") or ""
        else:
            roots = self.store.root_paths() if wants_files else []
            payload["hits"] = self.index.search(terms, limit=16, roots=roots) if roots else []
            payload["hitsSource"] = "local"
        return payload

    def active_sources(self) -> List[str]:
        """实际启用的联网源：单源开关 ∩ 它所属分类的总闸。
        三个总闸平级：联网管 web 组，娱乐管 fun 组（萌娘百科），互不连坐。"""
        switches = self.store.switches()
        web_on = bool(switches.get("web", True))
        fun_on = bool(switches.get("fun", True))
        return [n for n in self.store.enabled_sources()
                if (fun_on if n in FUN_SOURCES else web_on)]

    def web_only(self, term: str) -> Dict:
        """老实等全部源回来的老接口，留着当兜底。"""
        if not term:
            return {"term": term, "web": [], "trans": None}
        enabled = self.active_sources()
        kind = self.dict._kind(term)
        web = self.dict.web_lookup(term, True, enabled)
        trans = self.dict._merge_trans(term, kind, web,
                                       self.dict.translate(term, True, "DeepL" in enabled))
        return {"term": term, "web": web, "trans": trans,
                "sources": enabled}

    # -- 联网：一条条吐，别等所有源都回来 ---------------------------------
    def web_start(self, term: str) -> Dict:
        """开一次抓取。结果攒在 job 里，前端按 since 一条条取。"""
        term = (term or "").strip()
        enabled = self.active_sources()
        if not term or not enabled:
            return {"term": term, "items": [], "pending": [], "done": True,
                    "trans": None, "since": 0, "total": 0, "sources": enabled}

        key = term.lower()
        with self.web_lock:
            job = self.web_jobs.get(key)
            if job and not job["done"]:
                return self._job_view(job)
            job = {"term": term, "items": [], "pending": list(enabled),
                   "done": False, "trans": None, "at": time.time()}
            self.web_jobs[key] = job
            if len(self.web_jobs) > 15:      # 别把上次的词一直攒着
                for old in sorted(self.web_jobs, key=lambda k: self.web_jobs[k]["at"])[:-15]:
                    if old != key:
                        self.web_jobs.pop(old, None)

        def on_item(item: Dict) -> None:
            with self.web_lock:
                name = item.get("source")
                job["items"].append(item)
                job["pending"] = [n for n in job["pending"] if n != name]

        def run() -> None:
            try:
                items = self.dict.web_stream(term, on_item=on_item, sources=enabled)
                kind = self.dict._kind(term)
                trans = self.dict._merge_trans(
                    term, kind, items, self.dict.translate(term, True, "DeepL" in enabled))
            except Exception:
                trans = None
            with self.web_lock:
                job["trans"] = trans
                job["done"] = True
                job["pending"] = []

        threading.Thread(target=run, daemon=True).start()
        return self._job_view(job)

    def web_poll(self, term: str, since: int = 0) -> Dict:
        key = (term or "").strip().lower()
        with self.web_lock:
            job = self.web_jobs.get(key)
            if not job:
                return {"term": term, "items": [], "pending": [], "done": True,
                        "trans": None, "since": 0, "total": 0}
            items = [dict(item) for item in job["items"][since:]]
            return {"term": job["term"], "items": items,
                    "since": since + len(items),
                    "pending": list(job["pending"]),
                    "done": bool(job["done"]), "trans": job["trans"],
                    "total": len(job["items"])}

    def _job_view(self, job: Dict) -> Dict:
        items = [dict(item) for item in job["items"]]
        return {"term": job["term"], "items": items, "pending": list(job["pending"]),
                "done": bool(job["done"]), "trans": job["trans"],
                "since": len(items), "total": len(items)}

    @staticmethod
    def _clip_terms(term: str) -> List[str]:
        term = (term or "").strip()
        if not term:
            return []
        if len(term) <= 8:
            return [term]
        # 长句子：整句拿去做 LIKE 基本查不到（一句话很少原样出现在正文里）。
        # 英文按单词取；中文没有分词器，按三字窗口隔一个滑一个（step=2），
        # 「提示词」这种词必有一个窗口正好压住它。窗口里虚词太多就丢掉。
        chunks: List[str] = []
        for part in re.split(r"[\s，。！？、；：,.!?;:()（）\[\]\"'“”‘’/\\|]+", term):
            part = part.strip()
            if not part:
                continue
            chunks.extend(re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,30}", part))
            for seg in re.findall(r"[\u4e00-\u9fff]+", part):
                if len(seg) <= 4:
                    chunks.append(seg)
                    continue
                grams = [seg[i:i + 3] for i in range(0, len(seg) - 2, 2)]
                grams = [g for g in grams if sum(1 for ch in g if ch in STOP_CHARS) < 2]
                if len(grams) > 6:
                    step = len(grams) / 6.0
                    grams = [grams[int(i * step)] for i in range(6)]
                chunks.extend(grams)
        picked: List[str] = []
        for chunk in chunks:
            if chunk and chunk != term and chunk not in picked:
                picked.append(chunk)
            if len(picked) >= 8:
                break
        if not picked:
            return [term]
        return picked + [term]

    def search(self, term: str, scope: str = "roots") -> Dict:
        if not term:
            return {"scope": scope, "hits": [], "total": 0}
        if scope == "disk":
            hits = self._mdfind(term)
            return {"scope": scope, "hits": hits, "total": len(hits)}
        roots = self.store.root_paths()
        hits = self.index.search(self._clip_terms(term), limit=40, roots=roots) if roots else []
        return {"scope": "roots", "hits": hits, "total": len(hits)}

    def _mdfind(self, term: str) -> List[Dict]:
        try:
            result = subprocess.run(["mdfind", "-name", term], capture_output=True, timeout=8)
            paths = [p for p in result.stdout.decode("utf-8", "replace").split("\n") if p]
            if len(paths) < 5:
                result = subprocess.run(["mdfind", term], capture_output=True, timeout=10)
                paths = [p for p in result.stdout.decode("utf-8", "replace").split("\n") if p]
        except (OSError, subprocess.SubprocessError):
            return []
        home = str(Path.home())
        cleaned = []
        seen = set()
        for raw in paths:
            if any(raw.startswith(prefix) for prefix in SKIP_MDFIND):
                continue
            if raw in seen:
                continue
            seen.add(raw)
            cleaned.append(raw)
        cleaned.sort(key=lambda p: (not p.startswith(home), p.count("/"), p))
        out = []
        for raw in cleaned[:30]:
            path = Path(raw)
            try:
                stat = path.stat()
            except OSError:
                continue
            snippet = ""
            if len(out) < 8 and path.is_file() and stat.st_size < 4 * 1024 * 1024:
                text = extract.extract(path, limit=20000) or ""
                position = text.lower().find(term.lower())
                if position >= 0:
                    snippet = text[max(0, position - 60):position + 120].replace("\n", " ")
                    line = text.count("\n", 0, position) + 1
                else:
                    line = 1
                    snippet = text[:150].replace("\n", " ")
            else:
                line = 1
            out.append({"path": raw, "name": path.name, "dir": str(path.parent),
                        "ext": path.suffix.lower(), "line": line,
                        "snippet": snippet.strip(), "root": "disk"})
        return out

    # -- 查梗（娱乐网站）---------------------------------------------------
    def fun_on(self) -> bool:
        return bool(self.store.switches().get("fun", True))

    def meme_lookup(self, term: str) -> Dict:
        term = (term or "").strip()
        if not self.fun_on():
            return {"term": term, "result": None, "off": True}
        if not term:
            return {"term": term, "result": None}
        return {"term": term, "result": self.dict.meme_lookup(term)}

    def meme_sites(self) -> List[Dict]:
        return self.dict.meme_sites() if self.fun_on() else []

    def meme_sites_save(self, sites: List[Dict]) -> bool:
        try:
            path = self.dict.MEME_SITES_PATH
            path.write_text(json.dumps(sites, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except (OSError, TypeError, ValueError):
            return False

    # -- 动作 -------------------------------------------------------------
    def open_path(self, path: str, line: int = 0, reveal: bool = False) -> Dict:
        target = Path(os.path.expanduser(path))
        if not target.exists():
            raise ValueError("文件不在了：%s" % target.name)
        if line and self.command_block_status()["running"]:
            url = "xuhuancb://open?path=%s&line=%d" % (urllib.parse.quote(str(target)), line)
            try:
                result = subprocess.run(["open", url], timeout=4, capture_output=True)
                if result.returncode == 0:      # 命令方块接得住，才算跳成功
                    return {"ok": True, "how": "commandblock", "line": line}
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            if target.is_dir() or reveal:
                subprocess.run(["open", "-R", str(target)], timeout=6, capture_output=True)
                return {"ok": True, "how": "finder"}
            subprocess.run(["open", str(target)], timeout=6, capture_output=True)
            return {"ok": True, "how": "default"}
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("打不开：%s" % error)

    @staticmethod
    def pick_folder() -> str:
        script = ('set chosen to choose folder with prompt "选一个要登记的文件夹"\n'
                  'return POSIX path of chosen')
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", "replace").strip().rstrip("/")

    def shutdown(self) -> None:
        self.stop_flag = True
        self.index.close()


# --------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    service: SearchService = None
    server_version = "XuhuanSearch/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音：日志只写自己的文件
        pass

    # -- 工具 -------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> Dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        service = self.service

        if path == "/api/health":
            return self._json({"ok": True, "version": VERSION, "pid": os.getpid()})
        if path == "/api/state":
            return self._json(service.state())
        if path == "/api/clip":
            return self._json({"text": service.clip_text, "term": service.term_of(service.clip_text),
                               "at": service.clip_at, "seq": service.clip_seq})
        if path == "/api/lookup":
            term = (query.get("q") or [""])[0] or service.term_of(service.clip_text)
            use_web = (query.get("web") or ["0"])[0] == "1"
            return self._json(service.lookup(term, use_web=use_web))
        if path == "/api/web":
            term = (query.get("q") or [""])[0] or service.term_of(service.clip_text)
            return self._json(service.web_only(term))
        if path == "/api/web/poll":
            term = (query.get("q") or [""])[0]
            since = int((query.get("since") or ["0"])[0] or 0)
            return self._json(service.web_poll(term, since))
        if path == "/api/search":
            term = (query.get("q") or [""])[0]
            scope = (query.get("scope") or ["roots"])[0]
            return self._json(service.search(term, scope))
        if path == "/api/meme":
            term = (query.get("q") or [""])[0]
            return self._json(service.meme_lookup(term))
        if path == "/api/meme-sites":
            return self._json({"sites": service.meme_sites(), "off": not service.fun_on()})
        if path == "/api/changes":
            return self._json({"changes": service.index.changes(service.store.acked())})
        if path == "/api/pick-folder":
            picked = service.pick_folder()
            return self._json({"path": picked})
        if path == "/api/events":
            return self._stream(service)
        return self._static(path)

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        service = self.service
        body = self._body()
        try:
            if path == "/api/roots":
                action = body.get("action") or "add"
                if action == "add":
                    result = service.store.add_root(body.get("path") or "")
                    service.ensure_index(force=True)
                    return self._json({"ok": True, "result": result, "roots": service.store.roots()})
                if action == "remove":
                    result = service.store.remove_root(body.get("path") or "")
                    service.ensure_index(force=True)
                    return self._json({"ok": True, "result": result, "roots": service.store.roots()})
                if action == "reorder":
                    result = service.store.reorder_roots(body.get("order") or [])
                    return self._json(result)
                return self._error("action 只能是 add / remove / reorder")
            if path == "/api/switch":
                key = body.get("key") or ""
                updated = service.store.set_switch(key, body.get("value"))
                if key == "manage":
                    if body.get("value"):
                        service.index_abort.clear()
                        service.ensure_index(force=True)
                    else:
                        service.index_abort.set()   # 正在跑的扫描会在下一个文件处停下
                service.publish({"type": "switch", "switches": updated})
                return self._json({"ok": True, "switches": updated,
                                   "scope": service.store.effective_scope()})
            if path == "/api/source":
                # 单独关掉 / 打开联网里的某一个源
                updated = service.store.set_source(body.get("name") or "", body.get("value"))
                service.publish({"type": "source", "sources": updated,
                                 "switches": service.store.switches()})
                return self._json({"ok": True, "sources": updated})
            if path == "/api/web/start":
                return self._json(service.web_start(body.get("term") or ""))
            if path == "/api/reindex":
                service.ensure_index(force=True)
                return self._json({"ok": True})
            if path == "/api/copy":
                text = str(body.get("text") or "")
                if not text:
                    return self._error("没有要复制的内容")
                try:
                    subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=4)
                except (OSError, subprocess.SubprocessError):
                    return self._error("复制失败")
                return self._json({"ok": True})
            if path == "/api/ack":
                paths = body.get("paths") or []
                if body.get("all"):
                    paths = [c["path"] for c in service.index.changes(set())]
                paths = [str(p) for p in paths]
                service.index.rebase(paths)
                service.store.ack_clear()
                return self._json({"ok": True, "changes": service.index.changes(set())})
            if path == "/api/cb-new/ack":
                service.cb_ack_new()
                return self._json({"ok": True})
            if path == "/api/meme-sites":
                sites = body.get("sites")
                if not isinstance(sites, list):
                    return self._error("sites 必须是数组")
                ok = service.meme_sites_save(sites)
                return self._json({"ok": ok})
            if path == "/api/open":
                if body.get("url"):
                    url = str(body["url"])
                    if not re.match(r"^(https?|easydict|xuhuancb)://", url):
                        return self._error("只允许打开网页或已登记的深链接")
                    subprocess.run(["open", url], timeout=6, capture_output=True)
                    return self._json({"ok": True, "result": {"how": "url"}})
                result = service.open_path(body.get("path") or "", int(body.get("line") or 0),
                                           bool(body.get("reveal")))
                return self._json({"ok": True, "result": result})
            if path == "/api/quit":
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
                return self._json({"ok": True})
            if path == "/api/restart":
                if not spawn_successor(SERVER_PORT, service.store.data_dir):
                    return self._error("重启失败：无法拉起新服务", 500)
                # 先回 200，再让旧的这具退场；新服务会用同样的端口接管
                threading.Thread(target=lambda: (time.sleep(1.2), os._exit(0)), daemon=True).start()
                return self._json({"ok": True})
        except ValueError as error:
            return self._error(str(error), 422)
        except Exception as error:
            return self._error("服务出错：%s" % error, 500)
        return self._error("没有这个接口", 404)

    # -- 静态文件 ---------------------------------------------------------
    def _static(self, path: str) -> None:
        rel = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._error("越界了", 403)
        if not target.is_file():
            return self._error("找不到 %s" % rel, 404)
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        self._send(200, target.read_bytes(), mime)

    # -- SSE --------------------------------------------------------------
    def _stream(self, service: SearchService) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        box = service.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            last_beat = time.time()
            while not service.stop_flag:
                try:
                    event = box.get(timeout=15)
                    payload = "data: %s\n\n" % json.dumps(event, ensure_ascii=False)
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() - last_beat > 12:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            service.unsubscribe(box)


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def main() -> int:
    global RESTART_ARGV, SERVER_PORT
    parser = argparse.ArgumentParser(description="search 本地服务")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    # 记下原始启动参数，重启时用来拉起一个同配置的新进程
    RESTART_ARGV = list(sys.argv[1:])

    store = Store(Path(args.data_dir) if args.data_dir else None)
    reap_stale(store.data_dir)
    service = SearchService(store)
    if args.parent_pid:
        def watch_parent() -> None:
            while True:
                # 直接探 launcher 那个 PID 是否还活着，不比对自己的父进程——
                # 重启时新进程是老的进程拉起来的，老的退场后它会被系统改挂到 launchd，
                # 用 getppid 会比错对象而误自杀；探 launcher 的 PID 才是我们要的语义。
                try:
                    os.kill(args.parent_pid, 0)
                except ProcessLookupError:
                    service.shutdown()
                    os._exit(0)
                except OSError:
                    pass        # 权限类错误当成还活着，别误杀
                time.sleep(2)
        threading.Thread(target=watch_parent, daemon=True).start()

    port = args.port or free_port()
    Handler.service = service
    httpd = None
    for _ in range(40):                       # 重启时旧的还占着端口，等它让出来再绑
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError as exc:
            if getattr(exc, "errno", None) in (48, 98) or "Address already in use" in str(exc):
                time.sleep(0.3)
                continue
            raise
    if httpd is None:
        print("[search] 端口 %d 被占用，启动失败" % port, flush=True)
        sys.exit(1)
    httpd.daemon_threads = True
    SERVER_PORT = port

    threading.Thread(target=service.clipboard_loop, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(0.5), service.ensure_index()), daemon=True).start()
    # 命令方块如果开着，先把它的文件收进索引，这样「新增」一打开就是对的
    threading.Thread(target=lambda: (time.sleep(2.0), service.warm_command_block()), daemon=True).start()

    store.write_runtime(port)
    print("[search] 127.0.0.1:%d  version=%s  data=%s" % (port, VERSION, store.data_dir), flush=True)

    def stop(_signum, _frame):
        service.shutdown()
        store.clear_runtime()
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if not args.no_open:
        subprocess.run(["open", "http://127.0.0.1:%d/" % port], capture_output=True)

    try:
        httpd.serve_forever()
    finally:
        service.shutdown()
        store.clear_runtime()
    return 0


if __name__ == "__main__":
    sys.exit(main())
