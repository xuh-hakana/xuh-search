#!/usr/bin/env python3
"""search · 本地配置与运行时握手。

只做两件事：
1. 存用户的设置（登记了哪些文件夹、三个开关的状态、哪些变更已经被看过）；
2. 写一份 runtime.json 给「命令方块」看，让它知道 search 现在跑在哪个端口。

配置文件全在 ~/Library/Application Support/XuhuanSearch/，不往项目目录写任何东西。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "XuhuanSearch"
HOME = Path.home()
PROJECT_ROOT = HOME / "Documents" / "xuhuan mac"

# 首启不再预登记任何文件夹：她要的是「一开始就能搜整台电脑，登记过文件之后才只搜登记的」。
SEED_ROOTS = []

# 联网里的每一个源，都能单独关。DeepL 走的是网页接口、同 IP 问快了会 429，
# 所以默认关着；其余默认打开。
DEFAULT_SOURCES = {
    "百度百科": True,
    "维基百科": True,
    "萌娘百科": True,
    "MDN（前端文档）": True,
    "Stack Overflow": True,
    "GitHub": True,
    "npm（前端包）": True,
    "PyPI（Python 包）": True,
    "DeepL": False,
}

DEFAULT_STATE = {
    "version": 1,
    "roots": [],          # [{path, addedAt}]
    "switches": {
        "web": True,       # 【联网】联网抓取（所有联网源的总前提）
        "manage": True,    # 【管理】索引登记目录
        "fun": True,       # 【娱乐】萌娘百科 + 查梗 / 梗站目录
        "verbosity": "full",  # 【关/开/简】
        "pin": True,       # 窗口置顶
        "docked": False,   # 是否已并入命令方块
        "corner": "br",    # 并入时贴哪一角
        # "" 表示自动：登记过文件夹就只搜登记的，一个都没登记就搜整台电脑
        "scope": "",
        "sources": dict(DEFAULT_SOURCES),
    },
    "acked": [],          # 已看过的变更（路径）
    "seeded": False,
}


class Store:
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir or os.environ.get("SEARCH_DATA_DIR") or DEFAULT_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "state.json"
        self.runtime_path = self.data_dir / "runtime.json"
        self.lock = threading.RLock()
        self.state = self._load()

    # -- 基础读写 ---------------------------------------------------------
    def _load(self) -> Dict:
        data = json.loads(json.dumps(DEFAULT_STATE))  # 深拷贝
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key == "switches" and isinstance(value, dict):
                        data["switches"].update(value)
                        # sources 是「源名 → 开关」，新版本加的源要能补进老配置里
                        old_sources = value.get("sources")
                        merged = dict(DEFAULT_SOURCES)
                        if isinstance(old_sources, dict):
                            merged.update({k: bool(v) for k, v in old_sources.items()})
                        data["switches"]["sources"] = merged
                    elif key in data:
                        data[key] = value
        except (OSError, ValueError):
            pass
        if not data.get("seeded"):
            existing = {r.get("path") for r in data.get("roots") or []}
            for path in SEED_ROOTS:
                if Path(path).is_dir() and path not in existing:
                    data["roots"].append({"path": path, "addedAt": time.time()})
            data["seeded"] = True
            self.state = data
            self.save()
        return data

    def save(self) -> None:
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    # -- 登记文件夹 -------------------------------------------------------
    def roots(self) -> List[Dict]:
        with self.lock:
            out = []
            for item in self.state.get("roots") or []:
                path = str(Path(item.get("path", "")).expanduser())
                out.append({
                    "path": path,
                    "name": Path(path).name or path,
                    "exists": Path(path).is_dir(),
                    "addedAt": item.get("addedAt", 0),
                })
            return out

    def root_paths(self) -> List[str]:
        return [r["path"] for r in self.roots() if r["exists"]]

    def add_root(self, raw: str) -> Dict:
        text = (raw or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1]
        if not text:
            raise ValueError("先选一个文件夹")
        path = Path(os.path.expanduser(text))
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise ValueError("这个文件夹不存在，或者没有权限读")
        if not path.is_dir():
            raise ValueError("这是个文件，不是文件夹")
        if self.data_dir in path.parents or path == self.data_dir:
            raise ValueError("配置目录里有缓存，不能登记成搜索目录")
        with self.lock:
            existing = {r.get("path") for r in self.state["roots"]}
            if str(path) in existing:
                return {"added": False, "path": str(path)}
            self.state["roots"].append({"path": str(path), "addedAt": time.time()})
            self.save()
        return {"added": True, "path": str(path)}

    def remove_root(self, raw: str) -> Dict:
        text = str(Path(os.path.expanduser((raw or "").strip())))
        with self.lock:
            before = len(self.state["roots"])
            self.state["roots"] = [r for r in self.state["roots"] if r.get("path") != text]
            self.save()
            return {"removed": before - len(self.state["roots"]), "path": text}

    def reorder_roots(self, ordered_paths: List[str]) -> Dict:
        """按前端给的路径顺序重排登记目录；多出来/漏掉的都保底不丢。不改内容、不重建索引。"""
        with self.lock:
            by_path = {str(Path(os.path.expanduser((r.get("path") or "").strip()))): r
                       for r in self.state["roots"]}
            new_list = []
            seen = set()
            for p in (ordered_paths or []):
                key = str(Path(os.path.expanduser((p or "").strip())))
                if key in by_path and key not in seen:
                    new_list.append(by_path[key])
                    seen.add(key)
            # 保底：任何没出现在这次顺序里的原目录，原样接到末尾
            for r in self.state["roots"]:
                if r.get("path") not in seen:
                    new_list.append(r)
                    seen.add(r.get("path"))
            self.state["roots"] = new_list
            self.save()
        return {"ok": True, "roots": self.roots()}

    # -- 开关 -------------------------------------------------------------
    def switches(self) -> Dict:
        with self.lock:
            return dict(self.state.get("switches") or {})

    def set_switch(self, key: str, value) -> Dict:
        allowed = {"web", "manage", "fun", "verbosity", "pin", "docked", "corner", "scope"}
        if key not in allowed:
            raise ValueError("没有这个开关：%s" % key)
        if key == "scope" and value not in {"", "roots", "disk"}:
            raise ValueError("scope 只能是 roots / disk / 空")
        with self.lock:
            self.state["switches"][key] = value
            self.save()
            return dict(self.state["switches"])

    # -- 联网里的单个源 ---------------------------------------------------
    def sources(self) -> Dict[str, bool]:
        with self.lock:
            raw = self.state["switches"].get("sources")
            out = dict(DEFAULT_SOURCES)
            if isinstance(raw, dict):
                out.update({k: bool(v) for k, v in raw.items() if k in DEFAULT_SOURCES})
            return {name: out[name] for name in DEFAULT_SOURCES}   # 固定顺序

    def enabled_sources(self) -> List[str]:
        return [name for name, on in self.sources().items() if on]

    def set_source(self, name: str, value) -> Dict[str, bool]:
        if name not in DEFAULT_SOURCES:
            raise ValueError("没有这个服务：%s" % name)
        with self.lock:
            sources = self.sources()
            sources[name] = bool(value)
            self.state["switches"]["sources"] = sources
            self.save()
            return dict(sources)

    # -- 搜索范围（持久化，关掉软件再打开还是上次那个）---------------------
    def effective_scope(self) -> str:
        with self.lock:
            want = str(self.state["switches"].get("scope") or "")
        if want in {"roots", "disk"}:
            return want
        # 自动：登记过文件夹就只搜登记目录，一个都没登记就搜整台电脑
        return "roots" if self.root_paths() else "disk"

    # -- 变更已读 ---------------------------------------------------------
    def acked(self) -> set:
        with self.lock:
            return set(self.state.get("acked") or [])

    def ack(self, path: str) -> None:
        with self.lock:
            acked = set(self.state.get("acked") or [])
            acked.add(path)
            self.state["acked"] = sorted(acked)[-4000:]
            self.save()

    def ack_all(self, paths: List[str]) -> None:
        with self.lock:
            acked = set(self.state.get("acked") or [])
            acked.update(paths)
            self.state["acked"] = sorted(acked)[-4000:]
            self.save()

    def ack_clear(self) -> None:
        """基线已经按现在的文件状态重记过了，旧的「看过了」名单就没用了。"""
        with self.lock:
            if self.state.get("acked"):
                self.state["acked"] = []
                self.save()

    # -- 命令方块的新增文件 -----------------------------------------------
    def cb_seen_at(self) -> float:
        with self.lock:
            return float(self.state.get("cbSeenAt") or 0)

    def set_cb_seen_at(self, value: float) -> None:
        with self.lock:
            self.state["cbSeenAt"] = float(value)
            self.save()

    def cb_known(self) -> list:
        with self.lock:
            return list(self.state.get("cbKnown") or [])

    def set_cb_known(self, paths: list) -> None:
        with self.lock:
            self.state["cbKnown"] = list(paths)[:40000]
            self.save()

    # -- 运行时握手（给命令方块读）----------------------------------------
    def write_runtime(self, port: int, extra: Optional[Dict] = None) -> None:
        payload = {
            "app": "XuhuanSearch",
            "pid": os.getpid(),
            "port": port,
            "startedAt": time.time(),
            "version": 1,
        }
        if extra:
            payload.update(extra)
        tmp = self.runtime_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.runtime_path)

    def clear_runtime(self) -> None:
        try:
            self.runtime_path.unlink()
        except OSError:
            pass
