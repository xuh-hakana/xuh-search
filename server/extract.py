#!/usr/bin/env python3
"""search · 取文器：把各种文件里的字抠出来。

设计原则：
- 只用 macOS 自带的东西（python 标准库 + textutil + mdls），不装任何依赖；
- 抠不出来就返回 None，绝不抛异常炸掉整个索引；
- 单个文件最多取 MAX_CHARS 个字，避免一个巨型日志撑爆数据库。

覆盖：纯文本/代码/md/json/csv → 直接读；
      docx/xlsx/pptx/epub → 解 zip 抠 XML 文本；
      doc/rtf/docx 变体 → textutil 转；
      pdf → 自带最小解析器，抠不出来再用 Spotlight 的正文；
      图片/音视频 → 不取正文（只登记路径）。
"""

from __future__ import annotations

import os
import re
import subprocess
import zipfile
import zlib
from pathlib import Path
from typing import Optional

MAX_CHARS = 60_000
TEXT_EXT = {
    ".md", ".markdown", ".txt", ".text", ".rst", ".org", ".log", ".csv", ".tsv",
    ".json", ".jsonc", ".json5", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".properties", ".plist",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".less", ".sass",
    ".sh", ".zsh", ".bash", ".fish", ".ps1", ".bat", ".command",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".swift", ".go", ".rs",
    ".java", ".kt", ".rb", ".php", ".cs", ".lua", ".pl", ".r", ".jl", ".dart",
    ".sql", ".graphql", ".proto", ".diff", ".patch", ".gitignore", ".editorconfig",
}
ZIP_XML_EXT = {".docx", ".xlsx", ".pptx", ".epub", ".odt", ".ods", ".odp", ".pages"}
RTF_DOC_EXT = {".doc", ".rtf", ".rtfd", ".webarchive", ".html"}
SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "bower_components", "__pycache__",
    ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", ".next", ".nuxt",
    "dist", "build", ".build", ".cache", "Caches", ".Trash", "$RECYCLE.BIN",
    "Library", ".npm", ".yarn", ".gradle", ".idea", ".DS_Store",
}
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0]+")


def should_skip_dir(name: str) -> bool:
    # .app / .framework 是包，里面全是可执行文件和资源，没有任何「文字」值得进索引
    if name.endswith((".app", ".framework", ".bundle", ".xcodeproj")):
        return True
    return name in SKIP_DIRS or name.startswith(".")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def read_text_file(path: Path, limit: int = MAX_CHARS) -> Optional[str]:
    for encoding in ("utf-8", "utf-16", "gb18030", "big5", "latin-1"):
        try:
            with open(path, "r", encoding=encoding, errors="strict") as handle:
                return _clean(handle.read(limit * 2))[:limit]
        except UnicodeDecodeError:
            continue
        except (OSError, ValueError):
            return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return _clean(handle.read(limit * 2))[:limit]
    except OSError:
        return None


def _zip_xml_text(path: Path, limit: int = MAX_CHARS) -> Optional[str]:
    """docx/xlsx/pptx/epub 都是 zip 包，正文在 xml 里。"""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            picks = []
            for name in names:
                low = name.lower()
                if low.startswith("word/") and low.endswith(".xml"):
                    picks.append(name)
                elif low.startswith("xl/") and (low.endswith("sharedstrings.xml") or low.endswith(".xml")):
                    picks.append(name)
                elif low.startswith("ppt/slides/") and low.endswith(".xml"):
                    picks.append(name)
                elif low.endswith((".xhtml", ".html", ".htm")) and low.count("/") <= 2:
                    picks.append(name)
            if not picks:
                return None
            chunks = []
            total = 0
            for name in picks[:60]:
                try:
                    raw = archive.read(name).decode("utf-8", "replace")
                except (KeyError, OSError):
                    continue
                raw = raw.replace("</w:p>", "\n").replace("</a:p>", "\n")
                raw = raw.replace("</text:p>", "\n").replace("<w:tab/>", "\t")
                text = TAG_RE.sub(" ", raw)
                text = _clean(text)
                if text:
                    chunks.append(text)
                    total += len(text)
                if total >= limit:
                    break
            return _clean("\n".join(chunks))[:limit] or None
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None


def _pdf_unescape(raw: bytes) -> str:
    out = bytearray()
    index = 0
    length = len(raw)
    while index < length:
        byte = raw[index]
        if byte == 0x5C and index + 1 < length:  # 反斜杠
            nxt = raw[index + 1]
            mapping = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                       0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
            if nxt in mapping:
                out.append(mapping[nxt])
                index += 2
                continue
            if 0x30 <= nxt <= 0x37:  # 八进制
                digits = raw[index + 1:index + 4]
                keep = bytearray()
                for ch in digits:
                    if 0x30 <= ch <= 0x37 and len(keep) < 3:
                        keep.append(ch)
                    else:
                        break
                try:
                    out.append(int(keep.decode("ascii"), 8) & 0xFF)
                except ValueError:
                    pass
                index += 1 + len(keep)
                continue
            index += 1
            continue
        out.append(byte)
        index += 1
    return out.decode("utf-8", "replace")


def _pdf_stream_text(data: bytes) -> str:
    pieces = []
    for match in re.finditer(rb"\((?:\\.|[^\\()])*\)", data):
        tail = data[match.end():match.end() + 40]
        head = data[max(0, match.start() - 40):match.start()]
        if b"Tj" in tail[:12] or b"TJ" in tail[:24] or b"'" in tail[:12] or b"Tj" in head[-12:]:
            text = _pdf_unescape(match.group(0)[1:-1])
            if text:
                pieces.append(text)
    return " ".join(pieces)


def pdf_text(path: Path, limit: int = MAX_CHARS) -> Optional[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > 40 * 1024 * 1024:
        return _spotlight_text(path, limit)
    chunks = []
    total = 0
    for match in re.finditer(rb"stream\r?\n", raw):
        start = match.end()
        end = raw.find(b"endstream", start)
        if end <= start:
            continue
        blob = raw[start:end]
        try:
            data = zlib.decompress(blob)
        except zlib.error:
            data = blob
        if b"Tj" not in data and b"TJ" not in data:
            continue
        text = _pdf_stream_text(data)
        if text:
            chunks.append(text)
            total += len(text)
        if total >= limit:
            break
    merged = _clean(" ".join(chunks))
    # 中文 PDF 常用 CID 字体，抠出来是乱码 → 改问 Spotlight
    if len(merged) < 40 or _garbage_ratio(merged) > 0.35:
        fallback = _spotlight_text(path, limit)
        if fallback:
            return fallback
    return merged[:limit] or None


def _garbage_ratio(text: str) -> float:
    if not text:
        return 1.0
    bad = 0
    for ch in text[:2000]:
        code = ord(ch)
        if ch.isprintable() or ch in "\n\t":
            if 0xE000 <= code <= 0xF8FF:  # 私有区
                bad += 1
        else:
            bad += 1
    return bad / max(1, min(len(text), 2000))


def _spotlight_text(path: Path, limit: int = MAX_CHARS) -> Optional[str]:
    try:
        result = subprocess.run(
            ["mdls", "-raw", "-name", "kMDItemTextContent", str(path)],
            capture_output=True, timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.decode("utf-8", "replace").strip()
    if not out or out == "(null)":
        return None
    return _clean(out)[:limit] or None


def _textutil(path: Path, limit: int = MAX_CHARS) -> Optional[str]:
    try:
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            capture_output=True, timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _clean(result.stdout.decode("utf-8", "replace"))[:limit] or None


def extract(path: Path, limit: int = MAX_CHARS) -> Optional[str]:
    """取正文。取不到返回 None。"""
    suffix = path.suffix.lower()
    if suffix in TEXT_EXT or not suffix:
        return read_text_file(path, limit)
    if suffix in ZIP_XML_EXT:
        text = _zip_xml_text(path, limit)
        if text:
            return text
    if suffix == ".pdf":
        return pdf_text(path, limit)
    if suffix in RTF_DOC_EXT:
        return _textutil(path, limit) or read_text_file(path, limit)
    # 其它不认识的后缀，小文件试着当文本读一遍
    try:
        if path.stat().st_size <= 512 * 1024:
            return read_text_file(path, limit)
    except OSError:
        return None
    return None


def is_textish(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in TEXT_EXT or suffix in ZIP_XML_EXT or suffix in RTF_DOC_EXT or suffix == ".pdf"
