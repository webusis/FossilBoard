"""Git-привязка: коммиты с хэштегом #<task-id> → карточка.

Привязки не хранятся в board.json — они всегда читаются живьём из репозитория.
Иначе неизбежен рассинхрон: коммит, сделанный мимо доски, должен быть виден
доске так же, как и сделанный через неё.
"""

from __future__ import annotations

import re
import subprocess

MAX_DIFF_LINES = 4000
_LOG_FORMAT = "%H%x1f%h%x1f%aI%x1f%an%x1f%s"


class GitError(Exception):
    """Репозиторий недоступен или git не справился."""


def _git(repo_path: str, *args: str, timeout: int = 20) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:  # git не установлен
        raise GitError("git не найден в PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git завис: {' '.join(args)}") from exc
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} → {proc.returncode}")
    return proc.stdout


def is_repo(repo_path: str) -> bool:
    try:
        return _git(repo_path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def head_commit(repo_path: str) -> str | None:
    try:
        out = _git(repo_path, "rev-parse", "--short", "HEAD").strip()
        return out or None
    except GitError:
        return None


def is_stash(subject: str) -> bool:
    """Коммит git stash не привязан к задаче, даже если в тексте есть хэштег.

    Сообщения stash-коммитов выглядят как «WIP on master: <текст прошлого
    коммита>», «On master: …», «index on master: <sha> …» — и упоминание
    прошлого коммита с #FB-16 засчитывалось бы как ещё одна привязка,
    хотя к задаче отношения не имеет.
    """
    return subject.startswith(("WIP on ", "index on ", "On ", "Untracked files on "))


def commits_for_task(repo_path: str, task_id: str, limit: int = 50) -> list[dict]:
    """Найти коммиты, в сообщении которых встречается #<task-id>.

    Ищем по всему --all: ветка могла быть смержена, тег поставлен, cherry-pick
    сделан — привязка должна выживать такие истории.

    Хэштег с решёткой (#FB-3) — каноничная форма, но и без неё (FB-3: текст,
    как предлагает сама доска) считается привязкой. Границы выставлены явно,
    чтобы FB-3 не цеплял коммит FB-32.
    """
    escaped = re.escape(task_id)
    pattern = rf"(^|[^[:alnum:]-])#?{escaped}([^[:alnum:]-]|$)"
    out = _git(repo_path, "log", "--all", "-E", f"--grep={pattern}",
               f"--max-count={limit}", f"--pretty=format:{_LOG_FORMAT}")
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        full, short, date, author, subject = parts
        if is_stash(subject):
            continue
        commits.append({"hash": full, "short": short, "date": date,
                        "author": author, "subject": subject})
    return commits


def show_commit(repo_path: str, rev: str, max_lines: int = MAX_DIFF_LINES) -> dict:
    """Патч коммита. Длинный вывод честно обрезается и помечается."""
    out = _git(repo_path, "show", "--no-color", "--patch", rev, timeout=30)
    lines = out.splitlines()
    truncated = len(lines) > max_lines
    text = "\n".join(lines[:max_lines])
    if truncated:
        text += f"\n\n… вывод обрезан: показано {max_lines} из {len(lines)} строк."
    meta = {}
    if truncated:
        meta = _git(repo_path, "show", "--no-color", "-s", f"--pretty=format:{_LOG_FORMAT}",
                    rev, timeout=30).split("\x1f")
        meta = {"hash": meta[0], "short": meta[1], "date": meta[2],
                "author": meta[3], "subject": meta[4]} if len(meta) == 5 else {}
    return {"rev": rev, "text": text, "truncated": truncated,
            "total_lines": len(lines), "meta": meta}


def diff_stat(repo_path: str, rev: str) -> str:
    return _git(repo_path, "show", "--no-color", "--stat", "--oneline", rev, timeout=30)