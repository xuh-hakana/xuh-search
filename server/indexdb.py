#!/usr/bin/env python3
"""search · 本地索引。

一张表：docs(path, root, mtime, size, ext, title, body, lines, indexed_at)。
搜索用 LIKE 扫正文——不用分词器的原因很实际：
中文两字词（缓存、索引、字节）用 FTS5 的分词器要么切错、要么根本匹配不到，
LIKE 在几百个文件、每个正文最多 6 万字的情况下只要几十毫秒，够用且不会出错。
真要搜整台电脑，走系统的 Spotlight（mdfind），那条路是毫秒级。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

import extract

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  path TEXT PRIMARY KEY,
  root TEXT,
  mtime REAL,
  size INTEGER,
  ext TEXT,
  title TEXT,
  body TEXT,
  lines INTEGER,
  indexed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_docs_root ON docs(root);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Index:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.building = False
        self.progress = {"done": 0, "total": 0, "current": ""}
        self.last_build_at = 0.0

    # -- 元信息 -----------------------------------------------------------
    # 注意：sqlite3 的连接即使 check_same_thread=False 也不是线程安全的。
    # 建索引的线程在写、界面每几秒问一次状态在读，两边必须排队，
    # 否则会踩坏 page cache 直接 SIGSEGV（有过这个崩法）。
    def _set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO meta(k, v) VALUES(?, ?) "
                              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

    def _get_meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def stats(self) -> Dict:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM docs").fetchone()
            last = float(self._get_meta("last_build_at", "0") or 0)
            building = self.building
            progress = dict(self.progress)
        return {
            "files": row["n"] if row else 0,
            "bytes": row["bytes"] if row else 0,
            "lastBuildAt": last,
            "building": building,
            "progress": progress,
        }

    # -- 扫描 -------------------------------------------------------------
    def _walk(self, roots: List[str]) -> Iterable[str]:
        seen: Set[str] = set()
        for root in roots:
            base = Path(root)
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dirnames[:] = [d for d in dirnames if not extract.should_skip_dir(d)]
                if dirpath.count(os.sep) - str(base).count(os.sep) > 12:
                    dirnames[:] = []
                for name in filenames:
                    if name.startswith(".") and name not in {".env", ".gitignore"}:
                        continue
                    full = os.path.join(dirpath, name)
                    if full in seen:
                        continue
                    seen.add(full)
                    yield full

    def rebuild(self, roots: List[str],
                on_progress: Optional[Callable[[Dict], None]] = None,
                should_stop: Optional[Callable[[], bool]] = None) -> Dict:
        """全量重建。会删掉不在登记目录里的旧记录。"""
        with self.lock:
            if self.building:
                return {"ok": False, "reason": "正在建索引"}
            self.building = True
            self.progress = {"done": 0, "total": 0, "current": ""}
        started = time.time()
        added = updated = skipped = 0
        try:
            paths = list(self._walk(roots))
            total = len(paths)
            with self.lock:
                self.progress = {"done": 0, "total": total, "current": ""}
            keep: Set[str] = set()
            for position, raw in enumerate(paths, start=1):
                if should_stop and should_stop():
                    break
                path = Path(raw)
                keep.add(raw)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size > 24 * 1024 * 1024:
                    skipped += 1
                    continue
                with self.lock:
                    row = self.conn.execute("SELECT mtime, size FROM docs WHERE path=?", (raw,)).fetchone()
                if row and abs(row["mtime"] - stat.st_mtime) < 1 and row["size"] == stat.st_size:
                    continue
                body = extract.extract(path) or ""
                title = path.stem
                lines = body.count("\n") + 1 if body else 0
                with self.lock:
                    self.conn.execute(
                        "INSERT INTO docs(path, root, mtime, size, ext, title, body, lines, indexed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
                        "ext=excluded.ext, title=excluded.title, body=excluded.body, "
                        "lines=excluded.lines, indexed_at=excluded.indexed_at",
                        (raw, self._root_of(raw, roots), stat.st_mtime, stat.st_size,
                         path.suffix.lower(), title, body, lines, time.time()),
                    )
                    self.conn.commit()
                if row:
                    updated += 1
                else:
                    added += 1
                if position % 20 == 0:
                    with self.lock:
                        self.progress = {"done": position, "total": total,
                                         "current": str(path.relative_to(Path(roots[0])) if roots and str(path).startswith(str(roots[0])) else path.name)}
                if on_progress and position % 60 == 0:
                    on_progress(dict(self.progress))
            # 清掉已经不在登记目录里的记录
            with self.lock:
                rows = self.conn.execute("SELECT path FROM docs WHERE root NOT IN (%s)"
                                         % ",".join("?" * max(1, len(roots))),
                                         roots or [""]).fetchall()
                for row in rows:
                    if row["path"] not in keep:
                        self.conn.execute("DELETE FROM docs WHERE path=?", (row["path"],))
                self.conn.commit()
                self._set_meta("last_build_at", str(time.time()))
            self.last_build_at = time.time()
            return {"ok": True, "added": added, "updated": updated, "skipped": skipped,
                    "total": len(keep), "seconds": round(time.time() - started, 2)}
        finally:
            with self.lock:
                self.building = False

    def _root_of(self, raw: str, roots: List[str]) -> str:
        for root in roots:
            if raw == root or raw.startswith(root.rstrip("/") + "/"):
                return root
        return roots[0] if roots else ""

    # -- 变更检测 ---------------------------------------------------------
    def rebase(self, paths: List[str]) -> None:
        """把「已看过」的文件按下现在的 mtime 重新记基线 —— 以后它再变还会报。"""
        if not paths:
            return
        with self.lock:
            for path in paths:
                try:
                    stat = os.stat(path)
                except OSError:
                    self.conn.execute("DELETE FROM docs WHERE path = ?", (path,))
                    continue
                self.conn.execute("UPDATE docs SET mtime = ?, size = ? WHERE path = ?",
                                  (stat.st_mtime, stat.st_size, path))
            self.conn.commit()

    def changes(self, acked: Set[str]) -> List[Dict]:
        out: List[Dict] = []
        with self.lock:
            rows = self.conn.execute("SELECT path, root, mtime, size FROM docs").fetchall()
        for row in rows:
            path = row["path"]
            try:
                stat = os.stat(path)
            except OSError:
                if path not in acked:
                    out.append({"path": path, "root": row["root"], "kind": "missing",
                                "name": os.path.basename(path)})
                continue
            if abs(stat.st_mtime - row["mtime"]) > 1 or stat.st_size != row["size"]:
                if path not in acked:
                    out.append({"path": path, "root": row["root"], "kind": "changed",
                                "name": os.path.basename(path), "mtime": stat.st_mtime})
        out.sort(key=lambda item: item.get("mtime", 0), reverse=True)
        return out[:60]

    # -- 搜索 -------------------------------------------------------------
    def search(self, terms: List[str], limit: int = 24,
               roots: Optional[List[str]] = None) -> List[Dict]:
        terms = [t.strip() for t in terms if t and t.strip()]
        if not terms:
            return []
        # 多词：先按「全部命中」精确查；不够数再放宽成「命中任意一个」兜底。
        # 长句子拆出来的 4~6 个片段几乎不可能全命中，但只命中 2 个词也该算相关，
        # 所以兜底时按「命中词数」优先排序，而不是按 LIKE 的自然顺序。
        rows = self._select(terms, roots, require_all=True)
        if len(terms) > 1 and len(rows) < limit:
            seen = {row["path"] for row in rows}
            for row in self._select(terms, roots, require_all=False):
                if row["path"] not in seen:
                    rows.append(row)
                    seen.add(row["path"])
        lowered = [t.lower() for t in terms]
        hits: List[Dict] = []
        for row in rows:
            body = row["body"] or ""
            title = row["title"] or ""
            path = row["path"]
            low_body, low_title, low_path = body.lower(), title.lower(), path.lower()
            score = 0.0
            covered = 0
            for term in lowered:
                hit = False
                if term in low_title:
                    score += 60
                    hit = True
                if term in low_path:
                    score += 20
                    hit = True
                if term in low_body:
                    score += min(30, low_body.count(term) * 3)
                    hit = True
                if hit:
                    covered += 1
            index = -1
            for term in lowered:
                found = low_body.find(term)
                if found >= 0 and (index < 0 or found < index):
                    index = found
            if index < 0:
                snippet = (body[:160] or "").replace("\n", " ")
                line = 1
            else:
                start = max(0, index - 60)
                end = min(len(body), index + 140)
                snippet = body[start:end].replace("\n", " ")
                line = body.count("\n", 0, index) + 1
            hits.append({
                "path": path,
                "root": row["root"],
                "name": os.path.basename(path),
                "dir": os.path.dirname(path),
                "ext": row["ext"],
                "line": line,
                "snippet": snippet.strip(),
                "score": round(score, 1),
                "covered": covered,
            })
        # 兜底召回里只有 1 个词命中的噪音太大，直接丢掉（单词查询不设此限）
        floor = min(2, len(terms))
        hits = [item for item in hits if item["covered"] >= floor] if len(terms) > 1 else hits
        hits.sort(key=lambda item: (item["covered"], item["score"]), reverse=True)
        return hits[:limit]

    def _select(self, terms: List[str], roots: Optional[List[str]] = None,
                require_all: bool = True, cap: int = 400) -> List:
        """按词取行。require_all=False 时只要命中任意一个词就返回。"""
        where = ["(body LIKE ? OR title LIKE ? OR path LIKE ?)" for _ in terms]
        sql = ("SELECT path, root, body, ext, title FROM docs WHERE "
               + (" AND " if require_all else " OR ").join(where))
        params: List = []
        for term in terms:
            like = "%" + term + "%"
            params.extend([like, like, like])
        if roots:
            sql += " AND root IN (%s)" % ",".join("?" * len(roots))
            params.extend(roots)
        sql += " LIMIT %d" % cap
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
