#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import curses
from dataclasses import dataclass
from datetime import datetime
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence


VERSION = "0.1.0"


@dataclass
class RepoStatus:
    name: str
    path: str
    branch: str
    ahead: int
    behind: int
    staged_count: int
    unstaged_count: int
    untracked_count: int
    has_upstream: bool = True
    upstream_changed_at: str = ""
    error: str = ""


@dataclass
class FileChange:
    path: str
    status: str
    summary: str
    diff_path: str = ""


_COLORS_READY = False
COLOR_HEADER = 1
COLOR_CLEAN = 2
COLOR_DIRTY = 3
COLOR_DIVERGED = 4
COLOR_ERROR = 5
COLOR_FOOTER = 6


def _make_loading_status(path: str) -> RepoStatus:
    return RepoStatus(
        name=Path(path).name,
        path=path,
        branch="...",
        ahead=0,
        behind=0,
        staged_count=0,
        unstaged_count=0,
        untracked_count=0,
        has_upstream=False,
        upstream_changed_at="",
        error="loading",
    )


def _has_local_changes(status: RepoStatus) -> bool:
    return (
        status.staged_count > 0
        or status.unstaged_count > 0
        or status.untracked_count > 0
        or bool(status.error and status.error != "loading")
    )


def _visible_statuses(statuses: list[RepoStatus], dirty_only: bool) -> list[RepoStatus]:
    if not dirty_only:
        return statuses
    return [s for s in statuses if _has_local_changes(s)]


def _clamp_index(index: int, items: Sequence[object]) -> int:
    if not items:
        return 0
    if index < 0:
        return 0
    if index >= len(items):
        return len(items) - 1
    return index


def _safe_file_diff(status: RepoStatus, file_path: str) -> str:
    try:
        return get_file_diff(Path(status.path), file_path)
    except Exception as exc:
        return f"failed to load diff: {exc}"


def _load_repo_detail(status: RepoStatus) -> tuple[list[FileChange], int, str]:
    try:
        files = get_changed_files(Path(status.path))
    except Exception as exc:
        return [], 0, f"failed to load changed files: {exc}"

    if not files:
        return [], 0, "(no local file changes)"
    first = files[0]
    return files, 0, _safe_file_diff(status, first.diff_path or first.path)


def _select_file_and_load_diff(
    repo_status: RepoStatus,
    file_changes: list[FileChange],
    selected_file_idx: int,
) -> str:
    if not file_changes:
        return ""
    target = file_changes[selected_file_idx]
    return _safe_file_diff(repo_status, target.diff_path or target.path)


def find_git_repos(base_dir: str) -> list[Path]:
    base_path = Path(base_dir).expanduser().resolve()
    repos: list[Path] = []
    try:
        for child in sorted(base_path.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and (child / ".git").exists():
                repos.append(child)
    except OSError:
        return []
    return repos


def _run_git(
    repo_path: Path,
    *args: str,
    timeout: int = 5,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode not in allowed_returncodes:
        raise RuntimeError(proc.stderr.strip() or "git command failed")
    return proc.stdout.rstrip("\r\n")


def _status_porcelain_z(repo_path: Path) -> str:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo_path),
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--ignored=no",
        ],
        capture_output=True,
        text=False,
        check=False,
        timeout=5,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or "git command failed")
    return proc.stdout.decode("utf-8", errors="surrogateescape")


def _porcelain_z_entries(repo_path: Path) -> list[tuple[str, str, str, str | None]]:
    raw = _status_porcelain_z(repo_path)
    if not raw:
        return []

    entries: list[tuple[str, str, str, str | None]] = []
    tokens = raw.split("\0")
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token or len(token) < 3:
            continue
        x = token[0]
        y = token[1]
        path = token[3:] if len(token) > 3 else ""
        old_path: str | None = None
        if x in ("R", "C") and i < len(tokens):
            old_path = tokens[i] or None
            i += 1
        entries.append((x, y, path, old_path))
    return entries


def _parse_ahead_behind(raw: str) -> tuple[int, int]:
    parts = raw.split()
    if len(parts) != 2:
        return 0, 0
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
        return ahead, behind
    except ValueError:
        return 0, 0


def _upstream_changed_at(repo_path: Path) -> str:
    try:
        ts = _run_git(repo_path, "log", "-1", "--format=%ct", "@{upstream}")
        if not ts:
            return ""
        dt = datetime.fromtimestamp(int(ts))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _decode_status(code: str) -> str:
    mapping = {
        "M": "modified",
        "A": "added",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "U": "conflict",
        "?": "untracked",
    }
    return mapping.get(code, "changed")


def get_changed_files(repo_path: Path) -> list[FileChange]:
    changes: list[FileChange] = []
    for x, y, path, old_path in _porcelain_z_entries(repo_path):
        display_path = path or "(unknown)"
        diff_path = path or ""
        parts: list[str] = []
        if x == "?" and y == "?":
            parts.append("untracked")
        if x not in (" ", "?"):
            parts.append(f"staged {_decode_status(x)}")
        if y not in (" ", "?"):
            parts.append(f"unstaged {_decode_status(y)}")
        if not parts:
            parts.append("changed")
        if old_path and old_path != diff_path:
            parts.append(f"from {old_path}")
        changes.append(
            FileChange(
                path=display_path,
                status=f"{x}{y}",
                summary=", ".join(parts),
                diff_path=diff_path,
            )
        )
    return sorted(changes, key=lambda c: c.path.lower())


def get_file_diff(repo_path: Path, file_path: str) -> str:
    chunks: list[str] = []
    try:
        unstaged = _run_git(repo_path, "diff", "--", file_path, timeout=6)
    except RuntimeError:
        unstaged = ""
    if unstaged:
        chunks.append("## Unstaged\n" + unstaged)

    try:
        staged = _run_git(repo_path, "diff", "--cached", "--", file_path, timeout=6)
    except RuntimeError:
        staged = ""
    if staged:
        chunks.append("## Staged\n" + staged)

    if chunks:
        return "\n\n".join(chunks)

    file_on_disk = repo_path / file_path
    if file_on_disk.exists() and file_on_disk.is_file():
        try:
            return _run_git(
                repo_path,
                "diff",
                "--no-index",
                "--",
                "/dev/null",
                file_path,
                timeout=6,
                allowed_returncodes=(0, 1),
            )
        except RuntimeError:
            pass

    return "(no textual diff available for this file state)"


def get_repo_status(repo_path: Path) -> RepoStatus:
    name = repo_path.name
    try:
        branch = _run_git(repo_path, "branch", "--show-current") or "(detached)"
        has_upstream = True
        try:
            raw = _run_git(
                repo_path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"
            )
            ahead, behind = _parse_ahead_behind(raw)
            upstream_changed_at = _upstream_changed_at(repo_path)
        except RuntimeError:
            has_upstream = False
            ahead, behind = 0, 0
            upstream_changed_at = ""

        staged_count = 0
        unstaged_count = 0
        untracked_count = 0
        for x, y, _, _ in _porcelain_z_entries(repo_path):
            if x == "?" and y == "?":
                untracked_count += 1
                continue
            if x != " ":
                staged_count += 1
            if y != " ":
                unstaged_count += 1

        return RepoStatus(
            name=name,
            path=str(repo_path),
            branch=branch,
            ahead=ahead,
            behind=behind,
            staged_count=staged_count,
            unstaged_count=unstaged_count,
            untracked_count=untracked_count,
            has_upstream=has_upstream,
            upstream_changed_at=upstream_changed_at,
            error="",
        )
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        return RepoStatus(
            name=name,
            path=str(repo_path),
            branch="-",
            ahead=0,
            behind=0,
            staged_count=0,
            unstaged_count=0,
            untracked_count=0,
            has_upstream=False,
            upstream_changed_at="",
            error=str(exc),
        )


def update_current_branch(repo_path: Path) -> str:
    return _run_git(repo_path, "pull", "--ff-only", timeout=45)


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "~"


def _safe_addstr(
    stdscr: curses.window, y: int, x: int, text: str, width: int, color: int = 0
) -> None:
    if y < 0 or width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, color)
    except curses.error:
        return


def _init_colors() -> None:
    global _COLORS_READY
    if _COLORS_READY:
        return
    if not curses.has_colors():
        _COLORS_READY = True
        return
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
        curses.init_pair(COLOR_CLEAN, curses.COLOR_GREEN, -1)
        curses.init_pair(COLOR_DIRTY, curses.COLOR_YELLOW, -1)
        curses.init_pair(COLOR_DIVERGED, curses.COLOR_BLUE, -1)
        curses.init_pair(COLOR_ERROR, curses.COLOR_RED, -1)
        curses.init_pair(COLOR_FOOTER, curses.COLOR_MAGENTA, -1)
    except curses.error:
        pass
    _COLORS_READY = True


def _color_pair(idx: int) -> int:
    if not curses.has_colors():
        return 0
    return curses.color_pair(idx)


def _cell(text: str, width: int) -> str:
    return _fit(text, width).ljust(width)


def _short_name(name: str, width: int) -> str:
    if width <= 0 or len(name) <= width:
        return name
    if width <= 6:
        return _fit(name, width)
    left = (width - 1) // 2
    right = width - 1 - left
    return f"{name[:left]}~{name[-right:]}"


def _line_color(status: RepoStatus) -> int:
    if status.error:
        return _color_pair(COLOR_ERROR)
    if status.staged_count or status.unstaged_count or status.untracked_count:
        return _color_pair(COLOR_DIRTY)
    if status.ahead or status.behind:
        return _color_pair(COLOR_DIVERGED)
    return _color_pair(COLOR_CLEAN)


def _status_marker(status: RepoStatus) -> str:
    if status.error:
        return "ERR"
    if status.staged_count or status.unstaged_count or status.untracked_count:
        return "CHG"
    if status.ahead or status.behind:
        return "DIV"
    return "OK"


def _window_bounds(total: int, selected: int, rows: int) -> tuple[int, int]:
    if total <= 0 or rows <= 0:
        return 0, 0
    if total <= rows:
        return 0, total
    start = max(0, selected - (rows // 2))
    start = min(start, total - rows)
    return start, start + rows


def _list_row_values(status: RepoStatus) -> tuple[str, str, str, str, str, str]:
    upstream_at = "-"
    if status.error:
        upstream = "-"
        desc = f"repo error: {status.error}"
    else:
        upstream = (
            "no upstream"
            if not status.has_upstream
            else f"ahead {status.ahead}, behind {status.behind}"
        )
        upstream_at = status.upstream_changed_at or "-"
        desc = (
            f"staged {status.staged_count}, "
            f"unstaged {status.unstaged_count}, "
            f"untracked {status.untracked_count}"
        )

    return (
        status.name,
        status.branch,
        upstream,
        upstream_at,
        _status_marker(status),
        desc,
    )


def _compute_list_widths(
    statuses: list[RepoStatus], screen_w: int
) -> tuple[int, int, int, int, int, int]:
    headers = ("Repo", "Branch", "Upstream", "Upstream At", "Type", "Changes")
    repo_w = len(headers[0])
    branch_w = len(headers[1])
    upstream_w = len(headers[2])
    upstream_at_w = len(headers[3])
    type_w = len(headers[4])
    changes_w = len(headers[5])

    for s in statuses:
        repo, branch, upstream, upstream_at, marker, desc = _list_row_values(s)
        repo_w = max(repo_w, len(repo))
        branch_w = max(branch_w, len(branch))
        upstream_w = max(upstream_w, len(upstream))
        upstream_at_w = max(upstream_at_w, len(upstream_at))
        type_w = max(type_w, len(marker))
        changes_w = max(changes_w, len(desc))

    widths = {
        "repo": repo_w,
        "branch": branch_w,
        "upstream": upstream_w,
        "upstream_at": upstream_at_w,
        "type": type_w,
        "changes": changes_w,
    }
    mins = {
        "repo": 12,
        "branch": 10,
        "upstream": 12,
        "upstream_at": 10,
        "type": 4,
        "changes": 12,
    }

    total = sum(widths.values()) + 5
    shrink_order = ["changes", "branch", "repo", "upstream", "upstream_at", "type"]
    while total > screen_w:
        changed = False
        for key in shrink_order:
            if widths[key] > mins[key]:
                widths[key] -= 1
                total -= 1
                changed = True
                if total <= screen_w:
                    break
        if not changed:
            break

    return (
        widths["repo"],
        widths["branch"],
        widths["upstream"],
        widths["upstream_at"],
        widths["type"],
        widths["changes"],
    )


def _detail_rows(height: int) -> tuple[int, int]:
    body_h = max(0, height - 5)
    list_block_h = max(3, body_h // 3)
    diff_block_h = max(3, body_h - list_block_h - 1)
    if list_block_h + diff_block_h + 1 > body_h:
        diff_block_h = max(3, body_h - list_block_h)
    list_rows = max(1, list_block_h - 1)
    diff_rows = max(1, diff_block_h - 1)
    return list_rows, diff_rows


def render_screen(
    stdscr: curses.window,
    base_dir: str,
    statuses: list[RepoStatus],
    dirty_only: bool,
    last_action: str,
    selected_repo_idx: int,
    detail_mode: bool,
    file_changes: list[FileChange],
    selected_file_idx: int,
    diff_text: str,
    diff_scroll: int,
    current_repo_name: str,
    diff_focus: bool,
    show_help: bool,
) -> None:
    _init_colors()
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    mode = "DIRTY ONLY" if dirty_only else "ALL"
    pane = "DETAIL" if detail_mode else "LIST"
    repo_part = (
        f" | repo: {current_repo_name}" if detail_mode and current_repo_name else ""
    )
    header = f"Repo Changes TUI | {mode} | {pane}{repo_part} | base: {base_dir}"
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _safe_addstr(
        stdscr,
        0,
        0,
        _fit(f"{header} | updated: {updated_at}", width),
        width,
        _color_pair(COLOR_HEADER) | curses.A_BOLD,
    )

    top = 2
    body_h = max(0, height - 4)

    if not detail_mode:
        (
            repo_w,
            branch_w,
            upstream_w,
            upstream_at_w,
            type_w,
            changes_w,
        ) = _compute_list_widths(statuses, width)

        base_total = repo_w + branch_w + upstream_w + upstream_at_w + type_w + changes_w
        extra = width - (base_total + 5)
        col_sep = " "
        if extra >= 20:
            col_sep = "   "
        elif extra >= 10:
            col_sep = "  "

        cols = (
            f"{_cell('Repo', repo_w)}{col_sep}"
            f"{_cell('Branch', branch_w)}{col_sep}"
            f"{_cell('Upstream', upstream_w)}{col_sep}"
            f"{_cell('Upstream At', upstream_at_w)}{col_sep}"
            f"{_cell('Type', type_w)}{col_sep}"
            f"{_cell('Changes', changes_w)}"
        )
        _safe_addstr(stdscr, top, 0, _fit(cols, width), width, curses.A_BOLD)

        rows = max(0, body_h - 1)
        start, end = _window_bounds(len(statuses), selected_repo_idx, rows)
        for visible_row, idx in enumerate(range(start, end)):
            s = statuses[idx]
            row = top + 1 + visible_row
            repo_name, branch, upstream, upstream_at, marker, desc = _list_row_values(s)
            line = (
                f"{_cell(repo_name, repo_w)}{col_sep}"
                f"{_cell(branch, branch_w)}{col_sep}"
                f"{_cell(upstream, upstream_w)}{col_sep}"
                f"{_cell(upstream_at, upstream_at_w)}{col_sep}"
                f"{_cell(marker, type_w)}{col_sep}"
                f"{_cell(desc, changes_w)}"
            )
            color = _line_color(s)
            if idx == selected_repo_idx:
                color |= curses.A_REVERSE | curses.A_BOLD
            _safe_addstr(stdscr, row, 0, _fit(line, width), width, color)
    else:
        list_rows, diff_rows = _detail_rows(height)
        list_header_y = top
        list_start_y = list_header_y + 1
        diff_header_y = list_start_y + list_rows + 1
        diff_start_y = diff_header_y + 1

        files_hdr_attr = curses.A_BOLD
        diff_hdr_attr = curses.A_BOLD
        if diff_focus:
            diff_hdr_attr |= curses.A_REVERSE
        else:
            files_hdr_attr |= curses.A_REVERSE
        _safe_addstr(
            stdscr,
            list_header_y,
            0,
            _fit("Changed Files", width),
            width,
            files_hdr_attr,
        )

        sep_y = list_start_y + list_rows
        if 0 <= sep_y < height - 2:
            _safe_addstr(
                stdscr,
                sep_y,
                0,
                _fit("-" * max(1, width), width),
                width,
                _color_pair(COLOR_DIVERGED) | curses.A_BOLD,
            )

        start, end = _window_bounds(len(file_changes), selected_file_idx, list_rows)
        for visible_row, idx in enumerate(range(start, end)):
            c = file_changes[idx]
            row = list_start_y + visible_row
            color = _color_pair(COLOR_DIRTY)
            if idx == selected_file_idx:
                color |= curses.A_REVERSE | curses.A_BOLD
            _safe_addstr(
                stdscr, row, 0, _fit(f"{c.path} [{c.summary}]", width), width, color
            )

        selected_name = ""
        if file_changes:
            selected_name = file_changes[selected_file_idx].path
        diff_title = "Diff"
        if selected_name:
            diff_title = f"Diff: {selected_name}"
        _safe_addstr(
            stdscr, diff_header_y, 0, _fit(diff_title, width), width, diff_hdr_attr
        )

        lines = diff_text.splitlines() if diff_text else [""]
        start = min(max(0, diff_scroll), max(0, len(lines) - diff_rows))
        for i, line in enumerate(lines[start : start + diff_rows]):
            row = diff_start_y + i
            color = 0
            if (
                line.startswith("+++")
                or line.startswith("---")
                or line.startswith("@@")
            ):
                color = _color_pair(COLOR_DIVERGED)
            elif line.startswith("+"):
                color = _color_pair(COLOR_CLEAN)
            elif line.startswith("-"):
                color = _color_pair(COLOR_ERROR)
            _safe_addstr(stdscr, row, 0, _fit(line, width), width, color)

    focus_text = "focus=DIFF" if detail_mode and diff_focus else "focus=FILES"
    footer = f"[?] help  [arrows] nav  [enter/right] open/focus  [left/esc] back  [o] open IntelliJ  [u] update one  [Shift+u] update all  [r] refresh  [q] quit | {focus_text}"
    _safe_addstr(
        stdscr, height - 1, 0, _fit(footer, width), width, _color_pair(COLOR_FOOTER)
    )
    if height > 1:
        _safe_addstr(
            stdscr,
            height - 2,
            0,
            _fit(f"last action: {last_action}", width),
            width,
            _color_pair(COLOR_FOOTER),
        )

    if show_help:
        help_lines = [
            "Commands",
            f"  editor      {_active_editor_label()}",
            "  up/down     move selection (or diff scroll when diff focused)",
            "  enter/right open detail; in detail switch focus to diff",
            "  left/esc    back (diff -> files -> overview)",
            "  pgup/pgdn   scroll diff",
            "  a or d      toggle all/dirty",
            "  u           update selected/current repo",
            "  Shift+u     update all clean repos",
            "  o           open selected/current repo in IntelliJ",
            "  r           refresh git status now (selected/visible repos first, then rest)",
            "  ?           toggle this help",
            "  q           quit",
        ]
        box_w = max(40, min(width - 4, 88))
        box_h = min(len(help_lines) + 2, max(6, height - 4))
        start_y = max(1, (height - box_h) // 2)
        start_x = max(1, (width - box_w) // 2)

        for y in range(box_h):
            border_line = " " * box_w
            _safe_addstr(
                stdscr,
                start_y + y,
                start_x,
                border_line,
                box_w,
                _color_pair(COLOR_HEADER) | curses.A_REVERSE,
            )

        for i, line in enumerate(help_lines[: box_h - 2]):
            _safe_addstr(
                stdscr,
                start_y + 1 + i,
                start_x + 1,
                _fit(line, box_w - 2),
                box_w - 2,
                _color_pair(COLOR_HEADER) | curses.A_BOLD | curses.A_REVERSE,
            )

    stdscr.refresh()


def _update_clean_repos(statuses: list[RepoStatus]) -> tuple[str, int, list[str]]:
    clean = [s for s in statuses if not _has_local_changes(s) and s.has_upstream]
    if not clean:
        return "no clean repos to update", 0, []
    ok = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=min(8, len(clean))) as pool:
        futures = {
            pool.submit(update_current_branch, Path(s.path)): s.path for s in clean
        }
        for future in as_completed(futures):
            try:
                future.result()
                ok += 1
            except Exception:
                failed += 1
    return (
        f"updated clean repos: ok={ok} failed={failed}",
        len(clean),
        [s.path for s in clean],
    )


def _update_clean_repos_with_progress(
    stdscr: curses.window, statuses: list[RepoStatus]
) -> tuple[str, int, list[str]]:
    clean = [s for s in statuses if not _has_local_changes(s) and s.has_upstream]
    if not clean:
        return "no clean repos to update", 0, []

    ok = 0
    failed = 0
    total = len(clean)
    _render_busy_overlay(stdscr, f"Updating all clean repos... 0/{total}")

    with ThreadPoolExecutor(max_workers=min(8, total)) as pool:
        futures = {
            pool.submit(update_current_branch, Path(s.path)): s.path for s in clean
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                future.result()
                ok += 1
            except Exception:
                failed += 1
            _render_busy_overlay(
                stdscr,
                f"Updating all clean repos... {completed}/{total}  (ok:{ok} fail:{failed})",
            )

    return (
        f"updated clean repos: ok={ok} failed={failed}",
        total,
        [s.path for s in clean],
    )


def _update_single_repo(repo: RepoStatus) -> tuple[bool, str]:
    try:
        update_current_branch(Path(repo.path))
        return True, f"updated {repo.name}"
    except Exception as exc:
        return False, f"update failed for {repo.name}: {exc}"


def _open_in_editor(repo_path: str) -> tuple[bool, str]:
    base = Path(repo_path).resolve()
    try:
        root = _run_git(base, "rev-parse", "--show-toplevel")
        project_path = Path(root).resolve()
    except Exception:
        project_path = base

    path = str(project_path)
    editor = os.environ.get("REPO_CHANGES_TUI_EDITOR", "intellij").strip().lower()

    if editor in ("intellij", "idea", "ultimate", "intellij-ultimate"):
        try:
            proc = subprocess.run(
                ["idea", path],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if proc.returncode == 0:
                return True, f"opened in IntelliJ project: {Path(path).name}"
        except Exception:
            pass

        for app_name in ("IntelliJ IDEA", "IntelliJ IDEA Ultimate"):
            try:
                proc = subprocess.run(
                    ["open", "-na", app_name, "--args", path],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                if proc.returncode == 0:
                    return True, f"opened in IntelliJ project: {Path(path).name}"
            except Exception:
                continue

        return (
            False,
            "could not open IntelliJ (install 'idea' launcher or set REPO_CHANGES_TUI_EDITOR)",
        )

    # Custom editor command. Examples:
    # REPO_CHANGES_TUI_EDITOR=code
    # REPO_CHANGES_TUI_EDITOR='cursor --new-window'
    # REPO_CHANGES_TUI_EDITOR='open -a "Visual Studio Code" --args {path}'
    raw = os.environ.get("REPO_CHANGES_TUI_EDITOR", "")
    if not raw:
        return False, "editor is not configured"

    try:
        if "{path}" in raw:
            cmd = shlex.split(raw.replace("{path}", shlex.quote(path)))
        else:
            cmd = shlex.split(raw) + [path]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if proc.returncode == 0:
            return True, f"opened in {editor}: {Path(path).name}"
        return False, proc.stderr.strip() or f"failed to open editor '{editor}'"
    except Exception as exc:
        return False, f"failed to open editor '{editor}': {exc}"


def _active_editor_label() -> str:
    raw = os.environ.get("REPO_CHANGES_TUI_EDITOR", "").strip()
    if not raw:
        return "intellij (default)"
    return raw


def _render_busy_overlay(stdscr: curses.window, message: str) -> None:
    scr_h, scr_w = stdscr.getmaxyx()
    busy_w = max(1, min(scr_w - 2, 72))
    busy_h = 3
    by = max(0, (scr_h - busy_h) // 2)
    bx = max(0, (scr_w - busy_w) // 2)
    for y in range(busy_h):
        _safe_addstr(
            stdscr,
            by + y,
            bx,
            " " * busy_w,
            busy_w,
            _color_pair(COLOR_DIRTY) | curses.A_REVERSE,
        )
    _safe_addstr(
        stdscr,
        by + 1,
        bx + 1,
        _fit(message or "working...", busy_w - 2),
        busy_w - 2,
        _color_pair(COLOR_DIRTY) | curses.A_BOLD | curses.A_REVERSE,
    )
    stdscr.refresh()


def run(stdscr: curses.window, base_dir: str) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    try:
        curses.set_escdelay(25)
    except Exception:
        pass
    stdscr.nodelay(False)
    stdscr.timeout(100)

    repo_paths = [str(p) for p in find_git_repos(base_dir)]
    status_cache: dict[str, RepoStatus] = {}
    detail_cache: dict[str, tuple[list[FileChange], str]] = {}

    status_pool = ThreadPoolExecutor(max_workers=8)
    pending_status: dict[Future[RepoStatus], tuple[str, int]] = {}
    in_flight_by_path: dict[str, Future[RepoStatus]] = {}
    refresh_generation = 0

    def statuses_from_cache() -> list[RepoStatus]:
        return [
            status_cache.get(path, _make_loading_status(path)) for path in repo_paths
        ]

    def start_refresh(priority_paths: list[str]) -> None:
        nonlocal refresh_generation
        refresh_generation += 1
        generation = refresh_generation
        order: list[str] = []
        seen: set[str] = set()
        for p in priority_paths + repo_paths:
            if p and p not in seen:
                seen.add(p)
                order.append(p)

        for path in order:
            existing = in_flight_by_path.get(path)
            if existing is not None and not existing.done():
                pending_status[existing] = (path, generation)
                continue
            future = status_pool.submit(get_repo_status, Path(path))
            in_flight_by_path[path] = future
            pending_status[future] = (path, generation)

    def apply_pending_updates() -> None:
        done = [f for f in list(pending_status.keys()) if f.done()]
        for future in done:
            path, generation = pending_status.pop(future)
            if in_flight_by_path.get(path) is future:
                in_flight_by_path.pop(path, None)
            if generation != refresh_generation:
                continue
            try:
                status_cache[path] = future.result()
            except Exception as exc:
                status_cache[path] = RepoStatus(
                    name=Path(path).name,
                    path=path,
                    branch="-",
                    ahead=0,
                    behind=0,
                    staged_count=0,
                    unstaged_count=0,
                    untracked_count=0,
                    has_upstream=False,
                    upstream_changed_at="",
                    error=str(exc),
                )

    dirty_only = True
    last_action = "ready"
    selected_repo_idx = 0
    detail_mode = False
    detail_repo_path = ""
    diff_focus = False
    file_changes: list[FileChange] = []
    selected_file_idx = 0
    diff_text = ""
    diff_scroll = 0
    show_help = False
    show_busy = False
    busy_message = ""

    start_refresh(repo_paths)

    try:
        while True:
            apply_pending_updates()
            statuses = statuses_from_cache()
            visible = _visible_statuses(statuses, dirty_only)
            selected_repo_idx = _clamp_index(selected_repo_idx, visible)
            selected_file_idx = _clamp_index(selected_file_idx, file_changes)

            current_repo_name = ""
            if detail_mode and detail_repo_path:
                current_repo_name = status_cache.get(
                    detail_repo_path, _make_loading_status(detail_repo_path)
                ).name

            render_screen(
                stdscr,
                base_dir,
                visible,
                dirty_only,
                last_action,
                selected_repo_idx,
                detail_mode,
                file_changes,
                selected_file_idx,
                diff_text,
                diff_scroll,
                current_repo_name,
                diff_focus,
                show_help,
            )

            if show_busy:
                _render_busy_overlay(stdscr, busy_message)

            key = stdscr.getch()
            if key == -1:
                continue
            if key == ord("q"):
                break
            if key == ord("?"):
                show_help = not show_help
                continue
            if show_help and key == 27:
                show_help = False
                continue
            if show_help and key not in (ord("?"), ord("q")):
                show_help = False
                continue
            if key == 27 and not detail_mode:
                break
            if key == curses.KEY_RESIZE:
                continue

            if detail_mode and key == curses.KEY_NPAGE:
                diff_scroll += 20
                continue
            if detail_mode and key == curses.KEY_PPAGE:
                diff_scroll = max(0, diff_scroll - 20)
                continue

            if key in (curses.KEY_RIGHT, 10, 13):
                if not detail_mode and visible:
                    detail_mode = True
                    detail_repo_path = visible[selected_repo_idx].path
                    diff_focus = False
                    repo = status_cache.get(
                        detail_repo_path, _make_loading_status(detail_repo_path)
                    )
                    cached = detail_cache.get(repo.path)
                    if cached is not None:
                        file_changes, diff_text = cached
                        selected_file_idx = 0
                        last_action = "opened detail (cached)"
                    else:
                        file_changes, selected_file_idx, diff_text = _load_repo_detail(
                            repo
                        )
                        detail_cache[repo.path] = (file_changes, diff_text)
                        last_action = "opened detail"
                    diff_scroll = 0
                    continue
                if detail_mode:
                    diff_focus = True
                    last_action = "focus=diff"
                    continue

            if key in (curses.KEY_LEFT, 27):
                if detail_mode and diff_focus:
                    diff_focus = False
                    last_action = "focus=files"
                    continue
                if detail_mode:
                    detail_mode = False
                    detail_repo_path = ""
                    diff_focus = False
                    file_changes = []
                    selected_file_idx = 0
                    diff_text = (
                        "(detail closed; cached file list kept for quick reopen)"
                    )
                    diff_scroll = 0
                    last_action = "back to list"
                    continue

            if key == curses.KEY_UP:
                if detail_mode and diff_focus:
                    if diff_scroll > 0:
                        diff_scroll = max(0, diff_scroll - 1)
                    else:
                        old_idx = selected_file_idx
                        selected_file_idx = _clamp_index(
                            selected_file_idx - 1, file_changes
                        )
                        if selected_file_idx != old_idx:
                            repo = status_cache.get(
                                detail_repo_path, _make_loading_status(detail_repo_path)
                            )
                            diff_text = _select_file_and_load_diff(
                                repo, file_changes, selected_file_idx
                            )
                            lines = diff_text.splitlines() if diff_text else [""]
                            rows = _detail_rows(stdscr.getmaxyx()[0])[1]
                            diff_scroll = max(0, len(lines) - rows)
                    continue
                if detail_mode and file_changes:
                    old = selected_file_idx
                    selected_file_idx = _clamp_index(
                        selected_file_idx - 1, file_changes
                    )
                    if selected_file_idx != old:
                        repo = status_cache.get(
                            detail_repo_path, _make_loading_status(detail_repo_path)
                        )
                        diff_text = _select_file_and_load_diff(
                            repo, file_changes, selected_file_idx
                        )
                        lines = diff_text.splitlines() if diff_text else [""]
                        rows = _detail_rows(stdscr.getmaxyx()[0])[1]
                        diff_scroll = max(0, len(lines) - rows)
                    continue
                selected_repo_idx = _clamp_index(selected_repo_idx - 1, visible)
                continue

            if key == curses.KEY_DOWN:
                if detail_mode and diff_focus:
                    lines = diff_text.splitlines() if diff_text else [""]
                    rows = _detail_rows(stdscr.getmaxyx()[0])[1]
                    max_scroll = max(0, len(lines) - rows)
                    if diff_scroll < max_scroll:
                        diff_scroll += 1
                    else:
                        old_idx = selected_file_idx
                        selected_file_idx = _clamp_index(
                            selected_file_idx + 1, file_changes
                        )
                        if selected_file_idx != old_idx:
                            repo = status_cache.get(
                                detail_repo_path, _make_loading_status(detail_repo_path)
                            )
                            diff_text = _select_file_and_load_diff(
                                repo, file_changes, selected_file_idx
                            )
                            diff_scroll = 0
                    continue
                if detail_mode and file_changes:
                    old = selected_file_idx
                    selected_file_idx = _clamp_index(
                        selected_file_idx + 1, file_changes
                    )
                    if selected_file_idx != old:
                        repo = status_cache.get(
                            detail_repo_path, _make_loading_status(detail_repo_path)
                        )
                        diff_text = _select_file_and_load_diff(
                            repo, file_changes, selected_file_idx
                        )
                        diff_scroll = 0
                    continue
                selected_repo_idx = _clamp_index(selected_repo_idx + 1, visible)
                continue

            if key in (ord("a"), ord("d")):
                dirty_only = not dirty_only
                detail_mode = False
                detail_repo_path = ""
                diff_focus = False
                file_changes = []
                selected_file_idx = 0
                diff_text = ""
                diff_scroll = 0
                last_action = "mode=dirty" if dirty_only else "mode=all"
                continue

            if key == ord("u"):
                target_repo = None
                if detail_mode and detail_repo_path:
                    target_repo = status_cache.get(
                        detail_repo_path, _make_loading_status(detail_repo_path)
                    )
                elif visible:
                    target_repo = visible[selected_repo_idx]

                if target_repo is not None:
                    show_busy = True
                    busy_message = f"Updating {target_repo.name}..."
                    _render_busy_overlay(stdscr, busy_message)
                    ok, msg = _update_single_repo(target_repo)
                    show_busy = False
                    last_action = msg
                    if ok:
                        status_cache[target_repo.path] = get_repo_status(
                            Path(target_repo.path)
                        )
                        detail_cache.pop(target_repo.path, None)
                        if detail_mode and detail_repo_path == target_repo.path:
                            repo = status_cache[target_repo.path]
                            file_changes, selected_file_idx, diff_text = (
                                _load_repo_detail(repo)
                            )
                            diff_scroll = 0
                else:
                    last_action = "no repo selected to update"
                continue

            if key == ord("U"):
                show_busy = True
                busy_message = "Updating all clean repos..."
                _render_busy_overlay(stdscr, busy_message)
                msg, attempted, updated_paths = _update_clean_repos_with_progress(
                    stdscr, statuses
                )
                show_busy = False
                last_action = msg
                if attempted > 0:
                    start_refresh(updated_paths)
                    detail_cache.clear()
                continue

            if key == ord("o"):
                target_repo = None
                if detail_mode and detail_repo_path:
                    target_repo = status_cache.get(
                        detail_repo_path, _make_loading_status(detail_repo_path)
                    )
                elif visible:
                    target_repo = visible[selected_repo_idx]

                if target_repo is None:
                    last_action = "no repo selected to open"
                else:
                    ok, msg = _open_in_editor(target_repo.path)
                    last_action = msg
                continue

            if key == ord("r"):
                priority: list[str] = []
                if visible:
                    center = selected_repo_idx
                    low = max(0, center - 6)
                    high = min(len(visible), center + 6)
                    priority = [s.path for s in visible[low:high]]
                start_refresh(priority)
                detail_cache.clear()
                last_action = "refresh started (visible first)"
                continue
    except KeyboardInterrupt:
        return
    finally:
        status_pool.shutdown(wait=True, cancel_futures=True)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print("Usage: differ [base_dir]")
        print("       differ --version")
        return 0
    if args and args[0] in ("-V", "--version"):
        print(VERSION)
        return 0

    base_dir = Path(args[0]).expanduser() if args else Path.cwd()
    resolved_base = base_dir.resolve()
    if not resolved_base.exists() or not resolved_base.is_dir():
        print(f"Invalid base directory: {resolved_base}")
        return 1

    try:
        curses.wrapper(lambda stdscr: run(stdscr, str(resolved_base)))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
