#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session-recall: 零依赖本地历史会话粗略检索与精准定位工具
严格遵循 agent-blindfold (Heart-Under-Blade) 物理隔离规范：
  - 仅读取 ~/.gemini/antigravity/annotations/<uuid>.pbtxt
    与 ~/.gemini/antigravity/brain/<uuid>/.system_generated/logs/transcript.jsonl (及按需单行读取 transcript_full.jsonl)
  - 绝不扫描或触碰 .user_uploaded、tempmediaStorage 或任何二进制媒体文件。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PBTXT_TITLE_RE = re.compile(r'(?:^|[\s{;])title\s*:\s*"((?:[^"\\]|\\.)*)"')
PBTXT_SECONDS_RE = re.compile(r"last_user_view_time\s*:\s*\{\s*seconds\s*:\s*(\d+)")
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
STRIP_BLOCKS_RE = re.compile(
    r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE)>",
    re.DOTALL,
)


def _agy_root() -> Path:
    return Path.home() / ".gemini" / "antigravity"


def _unescape_pbtxt(raw_val: str) -> str:
    buf = bytearray()
    i, n = 0, len(raw_val)
    simple = {
        "n": ord("\n"),
        "r": ord("\r"),
        "t": ord("\t"),
        '"': ord('"'),
        "'": ord("'"),
        "\\": ord("\\"),
    }
    while i < n:
        ch = raw_val[i]
        if ch != "\\":
            buf.extend(ch.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= n:
            buf.append(ord("\\"))
            break
        esc = raw_val[i]
        if esc in "01234567":
            j = i
            while j < min(n, i + 3) and raw_val[j] in "01234567":
                j += 1
            buf.append(int(raw_val[i:j], 8) & 0xFF)
            i = j
        elif esc in ("x", "X") and i + 3 <= n and all(c in "0123456789abcdefABCDEF" for c in raw_val[i + 1 : i + 3]):
            buf.append(int(raw_val[i + 1 : i + 3], 16) & 0xFF)
            i += 3
        elif esc == "u" and i + 5 <= n and all(c in "0123456789abcdefABCDEF" for c in raw_val[i + 1 : i + 5]):
            buf.extend(chr(int(raw_val[i + 1 : i + 5], 16)).encode("utf-8", errors="replace"))
            i += 5
        elif esc in simple:
            buf.append(simple[esc])
            i += 1
        else:
            buf.extend(esc.encode("utf-8"))
            i += 1
    return buf.decode("utf-8", errors="replace")


def _load_annotations(ann_dir: Path) -> Dict[str, Tuple[str, Optional[float]]]:
    """返回 {conv_id: (title, view_epoch)}"""
    out: Dict[str, Tuple[str, Optional[float]]] = {}
    if not ann_dir.is_dir():
        return out
    for entry in os.scandir(ann_dir):
        if not entry.is_file() or not entry.name.endswith(".pbtxt"):
            continue
        cid = entry.name[:-6]
        if not UUID_RE.match(cid):
            continue
        try:
            raw = Path(entry.path).read_text(encoding="utf-8", errors="replace")
            m_title = PBTXT_TITLE_RE.search(raw)
            title = _unescape_pbtxt(m_title.group(1)).strip() if m_title else ""
            m_sec = PBTXT_SECONDS_RE.search(raw)
            sec = float(m_sec.group(1)) if m_sec else None
            out[cid.lower()] = (title, sec)
        except OSError:
            continue
    return out


def _clean_user_text(raw: str) -> str:
    if not raw:
        return ""
    m = USER_REQ_RE.search(raw)
    if m:
        return m.group(1).strip()
    cleaned = STRIP_BLOCKS_RE.sub("", raw)
    cleaned = re.sub(r"The following is a <SYSTEM_MESSAGE>.*", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _fmt_ts(iso_or_epoch: object) -> str:
    tz_cn = timezone(timedelta(hours=8))
    if isinstance(iso_or_epoch, (int, float)) and iso_or_epoch > 0:
        return datetime.fromtimestamp(float(iso_or_epoch), tz=timezone.utc).astimezone(tz_cn).strftime("%Y-%m-%d %H:%M")
    if isinstance(iso_or_epoch, str) and iso_or_epoch.strip():
        s = iso_or_epoch.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s).astimezone(tz_cn).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    return "unknown-time"


def _one_line(text: str, max_len: int = 140) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1] + "…"


def _extract_snippet(text: str, patterns: List[re.Pattern[str]], radius: int = 75) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return ""
    best_span: Optional[Tuple[int, int]] = None
    for pat in patterns:
        m = pat.search(flat)
        if m:
            best_span = m.span()
            break
    if not best_span:
        return _one_line(flat, radius * 2)
    start = max(0, best_span[0] - radius)
    end = min(len(flat), best_span[1] + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"


def _iter_transcripts(brain_dir: Path, days: Optional[int]) -> List[Tuple[str, Path, float]]:
    cutoff = (time.time() - days * 86400.0) if (days and days > 0) else 0.0
    items: List[Tuple[str, Path, float]] = []
    if not brain_dir.is_dir():
        return items
    for entry in os.scandir(brain_dir):
        if not entry.is_dir() or not UUID_RE.match(entry.name):
            continue
        t_path = Path(entry.path) / ".system_generated" / "logs" / "transcript.jsonl"
        try:
            st = t_path.stat()
        except OSError:
            continue
        if cutoff and st.st_mtime < cutoff:
            continue
        items.append((entry.name.lower(), t_path, st.st_mtime))
    items.sort(key=lambda x: x[2], reverse=True)
    return items


def cmd_recent(args: argparse.Namespace) -> int:
    root = _agy_root()
    anns = _load_annotations(root / "annotations")
    sessions = _iter_transcripts(root / "brain", args.days)
    limit = max(1, min(100, args.limit))

    print(f"=== 最近活跃会话 (Top {min(limit, len(sessions))} / 共 {len(sessions)} 个) ===")
    for cid, t_path, mtime in sessions[:limit]:
        title, _ = anns.get(cid, ("", None))
        first_prompt = ""
        line_count = 0
        try:
            with t_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_count += 1
                    if not first_prompt and '"USER_INPUT"' in line:
                        try:
                            obj = json.loads(line)
                            if obj.get("type") == "USER_INPUT":
                                first_prompt = _clean_user_text(obj.get("content") or "")
                        except json.JSONDecodeError:
                            pass
        except OSError:
            continue

        disp_title = title if title else "(无标题会话)"
        preview = _one_line(first_prompt, 90) if first_prompt else "(无用户输入)"
        print(f"• [{_fmt_ts(mtime)}] {cid} | {line_count:>3} 行 | {disp_title}")
        print(f"    首句: {preview}")
    return 0


@dataclass
class TurnHit:
    line_no: int
    step_idx: object
    role: str
    created_at: str
    truncated: bool
    matched_tokens: int
    snippet: str


@dataclass
class SessionHit:
    cid: str
    title: str
    t_path: Path
    mtime: float
    total_lines: int
    matched_token_set: set = field(default_factory=set)
    score: int = 0
    turn_hits: List[TurnHit] = field(default_factory=list)


def cmd_search(args: argparse.Namespace) -> int:
    raw_tokens = [t.strip() for t in args.query if t.strip()]
    if len(raw_tokens) == 1 and not args.regex:
        raw_tokens = [tok for tok in re.split(r"\s+", raw_tokens[0]) if tok]
    if not raw_tokens:
        print("请提供至少一个搜索关键词。", file=sys.stderr)
        return 1

    try:
        if args.regex:
            patterns = [re.compile(tok, re.IGNORECASE) for tok in raw_tokens]
        else:
            patterns = [re.compile(re.escape(tok), re.IGNORECASE) for tok in raw_tokens]
    except re.error as exc:
        print(f"正则表达式错误: {exc}", file=sys.stderr)
        return 1

    root = _agy_root()
    anns = _load_annotations(root / "annotations")
    sessions = _iter_transcripts(root / "brain", args.days)

    results: List[SessionHit] = []
    total_kw = len(patterns)

    session_prefixes = [
        p.strip().lower() for p in (args.session or "").split(",") if p.strip()
    ]

    for cid, t_path, mtime in sessions:
        if args.exclude and cid.startswith(args.exclude.strip().lower()):
            continue
        if session_prefixes and not any(cid.startswith(sp) for sp in session_prefixes):
            continue
        title, _ = anns.get(cid, ("", None))
        sh = SessionHit(cid=cid, title=title or "(无标题会话)", t_path=t_path, mtime=mtime, total_lines=0)
        token_hit_counts: Dict[int, int] = {}

        # 标题匹配加权
        if title:
            for idx, pat in enumerate(patterns):
                if pat.search(title):
                    sh.matched_token_set.add(idx)
                    token_hit_counts[idx] = token_hit_counts.get(idx, 0) + 1
                    sh.score += 8

        try:
            with t_path.open("r", encoding="utf-8", errors="replace") as f:
                for line_no, line in enumerate(f, start=1):
                    sh.total_lines = line_no
                    if '"USER_INPUT"' not in line and '"PLANNER_RESPONSE"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    stype = obj.get("type")
                    if stype == "USER_INPUT":
                        text = _clean_user_text(obj.get("content") or "")
                        role = "USER"
                        role_w = 3
                    elif stype == "PLANNER_RESPONSE":
                        text = (obj.get("content") or "").strip()
                        if args.thinking and obj.get("thinking"):
                            text = f"{text}\n[THINKING] {obj.get('thinking')}"
                        role = "ASSISTANT"
                        role_w = 1
                    else:
                        continue

                    if not text:
                        continue

                    hit_indices = [idx for idx, pat in enumerate(patterns) if pat.search(text)]
                    if not hit_indices:
                        continue

                    sh.matched_token_set.update(hit_indices)
                    # BM25 风格词频饱和 (单会话每词最多计 3 次基础分) + 同轮多词共现平方奖励
                    for idx in hit_indices:
                        prev = token_hit_counts.get(idx, 0)
                        token_hit_counts[idx] = prev + 1
                        if prev < 3:
                            sh.score += role_w * 2
                    if len(hit_indices) >= 2:
                        sh.score += (len(hit_indices) ** 2) * (4 if role == "USER" else 2)

                    matched_pats = [patterns[idx] for idx in hit_indices]
                    sh.turn_hits.append(
                        TurnHit(
                            line_no=line_no,
                            step_idx=obj.get("step_index", "?"),
                            role=role,
                            created_at=_fmt_ts(obj.get("created_at") or mtime),
                            truncated=bool(obj.get("truncated_fields")),
                            matched_tokens=len(hit_indices),
                            snippet=_extract_snippet(text, matched_pats),
                        )
                    )
        except OSError:
            continue

        if not sh.matched_token_set:
            continue
        if args.all and len(sh.matched_token_set) < total_kw:
            continue

        # 会话内片段按 (命中词数, USER优先, 行号) 排序，保留最精准的前 max_snippets 条
        sh.turn_hits.sort(key=lambda h: (h.matched_tokens, 1 if h.role == "USER" else 0, -h.line_no), reverse=True)
        results.append(sh)

    results.sort(key=lambda s: (len(s.matched_token_set), s.score, s.mtime), reverse=True)
    limit = max(1, min(50, args.limit))
    max_snips = max(1, min(15, args.snippets))

    print(
        f"=== 检索词: {raw_tokens} | 命中会话: {len(results)} 个 (展示前 {min(limit, len(results))} 个) ==="
    )
    if not results:
        return 0

    for rank, sh in enumerate(results[:limit], start=1):
        print(
            f"\n[{rank}] {sh.title}  (命中词 {len(sh.matched_token_set)}/{total_kw} · 权重 {sh.score})"
        )
        print(f"    会话链接 : conversation://{sh.cid}")
        print(f"    活跃时间 : {_fmt_ts(sh.mtime)}  |  总行数: {sh.total_lines}")
        print(f"    日志路径 : {sh.t_path}")
        shown = sorted(sh.turn_hits[:max_snips], key=lambda h: h.line_no)
        for th in shown:
            trunc_tag = " [有折叠->可查 transcript_full.jsonl]" if th.truncated else ""
            print(
                f"      - Line {th.line_no:<4} (Step {th.step_idx:<3} · {th.role:<9} · {th.created_at}){trunc_tag}:"
            )
            print(f"        {th.snippet}")
    return 0


def _resolve_session(brain_dir: Path, prefix: str) -> Optional[Tuple[str, Path]]:
    q = prefix.strip().lower()
    if not q:
        return None
    matches: List[Tuple[str, Path, float]] = []
    if not brain_dir.is_dir():
        return None
    for entry in os.scandir(brain_dir):
        if not entry.is_dir() or not UUID_RE.match(entry.name):
            continue
        cid = entry.name.lower()
        if cid == q or cid.startswith(q):
            t_path = Path(entry.path) / ".system_generated" / "logs" / "transcript.jsonl"
            if t_path.is_file():
                matches.append((cid, t_path, t_path.stat().st_mtime))
    if not matches:
        return None
    matches.sort(key=lambda x: x[2], reverse=True)
    return matches[0][0], matches[0][1]


def cmd_show(args: argparse.Namespace) -> int:
    root = _agy_root()
    resolved = _resolve_session(root / "brain", args.session)
    if not resolved:
        print(f"未找到匹配的会话 ID 或前缀: {args.session}", file=sys.stderr)
        return 1

    cid, t_path = resolved
    if args.full:
        full_candidate = t_path.with_name("transcript_full.jsonl")
        if full_candidate.is_file():
            t_path = full_candidate

    anns = _load_annotations(root / "annotations")
    title, _ = anns.get(cid, ("(无标题会话)", None))

    MAX_SAFE_SPAN = 40
    span_clamped_msg = ""
    start_line, end_line = 1, 10**9

    if args.tail and args.tail > 0:
        total_lines = 0
        with t_path.open("r", encoding="utf-8", errors="replace") as f_cnt:
            for total_lines, _ in enumerate(f_cnt, start=1):
                pass
        tail_n = min(args.tail, MAX_SAFE_SPAN) if not args.allow_wide else args.tail
        start_line = max(1, total_lines - tail_n + 1)
        end_line = max(1, total_lines)
    elif args.lines:
        m = re.match(r"^(\d+)(?::|-|\.\.)(\d+)$", args.lines.strip())
        if not m:
            print("--lines 格式应为 START:END（例如 35:50）", file=sys.stderr)
            return 1
        start_line, end_line = int(m.group(1)), int(m.group(2))
        if end_line < start_line:
            start_line, end_line = end_line, start_line
        if not args.allow_wide and (end_line - start_line + 1) > MAX_SAFE_SPAN:
            orig_end = end_line
            end_line = start_line + MAX_SAFE_SPAN - 1
            span_clamped_msg = (
                f"[防撑爆物理限位] 请求行区间 {start_line}..{orig_end} 超过单次上限 ({MAX_SAFE_SPAN} 行)，"
                f"已自动截断至 {start_line}..{end_line}。\n"
                f"  -> 请优先用 `recall.py search <关键词> --session {cid[:8]}` 锁定精确行号后再 `--around-line` 切片精读！"
            )
    elif args.around_line:
        ctx = max(1, min(20, args.context))
        start_line = max(1, args.around_line - ctx)
        end_line = args.around_line + ctx

    max_chars = 8000 if args.full else 900
    print(f"=== 会话详情: {title} ({cid}) ===")
    print(f"    读取文件: {t_path} (行范围: {start_line}..{end_line if end_line < 10**8 else 'END'})")
    if span_clamped_msg:
        print(f"    {span_clamped_msg}")
    print()

    printed = 0
    with t_path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, start=1):
            if line_no < start_line:
                continue
            if line_no > end_line:
                break
            if '"USER_INPUT"' not in line and '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            stype = obj.get("type")
            if stype == "USER_INPUT":
                body = _clean_user_text(obj.get("content") or "")
                role = "USER"
            elif stype == "PLANNER_RESPONSE":
                body = (obj.get("content") or "").strip()
                tcs = obj.get("tool_calls") or []
                if not body and tcs:
                    tc_names = ", ".join(str(tc.get("name", "?")) for tc in tcs if isinstance(tc, dict))
                    body = f"[调用工具: {tc_names}]"
                role = "ASSISTANT"
            else:
                continue
            if not body:
                continue
            trunc_note = " [原字段有折叠，可加 --full 查看无损版]" if obj.get("truncated_fields") and not args.full else ""
            if len(body) > max_chars:
                body = body[:max_chars] + f"\n... [已截断，共 {len(body)} 字符；加 --full 或用 read_file 查看完整内容]"
            print(f"--- [Line {line_no} | Step {obj.get('step_index', '?')} | {role} | {_fmt_ts(obj.get('created_at'))}]{trunc_note} ---")
            print(body)
            print()
            printed += 1
            if not args.lines and not args.around_line and not args.tail and printed >= 8:
                print(
                    f"... [未指定行范围，默认仅预览开头 8 轮对话；请用 `search <关键词> --session {cid[:8]}` 定位行号，或用 `--tail 15` 看结尾结论]"
                )
                break
    if span_clamped_msg:
        print(span_clamped_msg)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Antigravity 本地会话粗略检索与精准定位工具")
    sub = p.add_subparsers(dest="subcmd", required=True)

    p_recent = sub.add_parser("recent", help="按最近活跃时间列出本地会话标题与首句预览")
    p_recent.add_argument("--limit", type=int, default=15, help="返回条数上限 (默认 15)")
    p_recent.add_argument("--days", type=int, default=0, help="仅看最近 N 天 (默认 0=不限)")
    p_recent.set_defaults(func=cmd_recent)

    p_search = sub.add_parser("search", help="按多关键词权重粗略检索所有本地会话与对应行号")
    p_search.add_argument("query", nargs="+", help="检索关键词（空格分隔多个词，自动按命中数与角色加权排序）")
    p_search.add_argument("--days", type=int, default=0, help="仅搜最近 N 天 (默认 0=全部)")
    p_search.add_argument("--limit", type=int, default=8, help="最多展示几个命中会话 (默认 8)")
    p_search.add_argument("--snippets", type=int, default=4, help="每个会话最多展示几条命中摘要 (默认 4)")
    p_search.add_argument("--all", action="store_true", help="要求同时命中全部关键词 (默认允许部分命中并按命中数降序)")
    p_search.add_argument("--regex", action="store_true", help="将关键词作为正则表达式匹配")
    p_search.add_argument("--thinking", action="store_true", help="同时检索模型的内部 thinking 字段")
    p_search.add_argument("--session", type=str, default="", help="仅在指定会话 UUID 或前缀内检索（多个用逗号分隔，如 4a736f12）")
    p_search.add_argument("--exclude", type=str, default="", help="排除当前会话 UUID 或前缀（如 2f5abc32）")
    p_search.set_defaults(func=cmd_search)

    p_show = sub.add_parser("show", help="按会话 UUID（支持前缀）与行号切片展开纯净对话上下文")
    p_show.add_argument("session", help="会话 UUID 或前几位前缀（如 8c6ff003）")
    p_show.add_argument("--around-line", type=int, default=0, help="围绕指定行号展开上下文（推荐首选）")
    p_show.add_argument("--context", type=int, default=5, help="配合 --around-line 前后展开的行数半径 (默认 5，上限 20)")
    p_show.add_argument("--lines", type=str, default="", help="指定小范围行号区间 START:END（默认单次跨度上限 40 行）")
    p_show.add_argument("--tail", type=int, default=0, help="仅读取会话末尾最后 N 行（用于无关键词时快速查看收尾结论）")
    p_show.add_argument("--allow-wide", action="store_true", help="解除单次 40 行跨度物理限位（仅用户显式要求导出全量对话时允许）")
    p_show.add_argument("--full", action="store_true", help="切换读取无损 transcript_full.jsonl 并放宽单步截断长度")
    p_show.set_defaults(func=cmd_show)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
