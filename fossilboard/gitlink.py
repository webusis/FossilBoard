"""Git-привязка: коммиты с хэштегом #<task-id> → карточка.

Привязки не хранятся в board.json — они всегда читаются живьём из репозитория.
Иначе неизбежен рассинхрон: коммит, сделанный мимо доски, должен быть виден
доске так же, как и сделанный через неё.

Здесь же разбор флагов-граблей из сообщений коммитов (FB-903):
    FB-12 #rake Тк не thread-safe: configure из фонового потока убивает окно
"""

from __future__ import annotations

import re
import subprocess

MAX_DIFF_LINES = 4000
_LOG_FORMAT = "%H%x1f%h%x1f%aI%x1f%an%x1f%s"
# полное сообщение коммита (тело может быть многострочным) — записи разделены
# record separator, поля — unit separator
_LOG_FORMAT_FULL = "%H%x1f%h%x1f%aI%x1f%an%x1f%B%x1e"

# Флаги-грабли по умолчанию. Реестр расширяется конфигом машины:
# в ~/.config/fossilboard/config.json → "flags": {"мой-флаг": "что это значит"}
DEFAULT_FLAGS = {
    "rake": "грабля или урок: что не сработало и почему",
    "gotcha": "неочевидное поведение, о котором стоит знать",
    "fix": "как починили",
    "todo": "долг, который остался",
}


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


# --------------------------------------------------------------------------
# флаги-грабли из сообщений коммитов (FB-903)
# --------------------------------------------------------------------------

def parse_flags(text: str, flags: dict | None = None, anchored: bool = False) -> list[dict]:
    """Вытащить флаги-грабли из строки текста.

        FB-12 #rake Тк не thread-safe  →  [{"flag": "rake", "text": "Тк не thread-safe"}]

    Правила:
    - после флага обязателен разделитель: «#rake текст». Перечисление
      «#rake/#gotcha/#fix/#todo» граблями не считается;
    - текст грабли — до конца строки и до следующего хэштега;
    - anchored=True — флаг должен стоять в начале строки (для тела коммита:
      пример в коде не должен цепляться как грабля).
    """
    registry = DEFAULT_FLAGS if flags is None else flags
    if not registry:
        return []
    # перечисление флагов («#rake/#gotcha/#fix/#todo») — не грабли:
    # после флага там нет разделителя, а текст рядом — обычное слово
    cleaned = re.sub(r"#[A-Za-z0-9_-]+(?:/#[A-Za-z0-9_-]+)+", " ", text)
    names = sorted(registry, key=len, reverse=True)   # длинные первыми: #gotcha важнее #go
    head = r"^[ \t]*" if anchored else r"(?:^|(?<=[\s(]))"
    finder = re.compile(
        head + r"#(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_-])",
        re.IGNORECASE | re.MULTILINE)
    hits = list(finder.finditer(cleaned))
    out: list[dict] = []
    for i, hit in enumerate(hits):
        start = hit.end()
        line_end = cleaned.find("\n", start)
        if line_end == -1:
            line_end = len(cleaned)
        # конец текста грабли: следующий флаг, следующий хэштег или конец строки
        if i + 1 < len(hits) and hits[i + 1].start() < line_end:
            end = hits[i + 1].start()
        else:
            nxt = re.search(r"#[A-Za-z0-9_-]+", cleaned[start:line_end])
            end = start + nxt.start() if nxt else line_end
        chunk = cleaned[start:end]
        # после флага обязателен разделитель: «#rake текст», а не «#rake/»
        if chunk and not chunk[0].isspace():
            continue
        chunk = chunk.strip(" \t-–—:;,")
        if chunk:
            out.append({"flag": hit.group(1).lower(), "text": chunk})
    return out


def card_ids_in(text: str, prefix: str) -> list[str]:
    """Номера карточек, упомянутые в тексте: «#FB-12» и «FB-12: текст».

    Границы выставлены явно, чтобы FB-3 не цеплял FB-32. Классы — Python'овские:
    POSIX-выражения вроде [:alnum:] из git -E тут не работают.
    """
    pattern = rf"(?<![A-Za-z0-9_-])#?{re.escape(prefix)}-(\d+)(?![A-Za-z0-9_-])"
    found: list[str] = []
    for match in re.finditer(pattern, text):
        card = f"{prefix}-{match.group(1)}"
        if card not in found:
            found.append(card)
    return found


def commits_with_flags(repo_path: str, flags: dict | None = None,
                       limit: int = 500) -> list[dict]:
    """Коммиты репы, в сообщении которых есть флаги-грабли.

    Читается полное сообщение (тело коммита тоже) — грабля может быть
    описана в теле, а не в заголовке.
    """
    registry = DEFAULT_FLAGS if flags is None else flags
    if not registry:
        return []
    out = _git(repo_path, "log", "--all", f"--max-count={limit}",
               f"--pretty=format:{_LOG_FORMAT_FULL}")
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f", 4)
        if len(parts) != 5:
            continue
        full, short, date, author, message = parts
        if is_stash(message.splitlines()[0] if message.splitlines() else ""):
            continue
        # заголовок — флаг где угодно; тело — только если строка начинается
        # с флага, иначе примеры в коде цеплялись бы как грабли
        lines = message.splitlines()
        found = parse_flags(lines[0], registry) if lines else []
        for line in lines[1:]:
            found += parse_flags(line, registry, anchored=True)
        if not found:
            continue
        commits.append({
            "hash": full, "short": short, "date": date, "author": author,
            "subject": message.strip().splitlines()[0] if message.strip() else "",
            "message": message,
            "flags": found,
        })
    return commits


def collect_gotchas(repo_path: str, data: dict, flags: dict | None = None,
                    only: str | None = None) -> list[dict]:
    """Грабли из истории репы: [{card_id, flag, text, commit}].

    Флаг без привязки к карточке не принимается: сначала ищем в сообщении
    карточку, и только потом флаги. Порядок в тексте значения не имеет.
    """
    registry = DEFAULT_FLAGS if flags is None else flags
    if not registry:
        return []
    prefix = (data.get("project", {}).get("config", {}).get("task_prefix")
              or data.get("project", {}).get("id") or "TASK")
    out: list[dict] = []
    for commit in commits_with_flags(repo_path, registry):
        # карточка ищется в заголовке коммита — как и привязка. Иначе пример
        # «git commit -m "FB-12 #rake …"» в теле притянул бы чужую карточку
        cards = card_ids_in(commit["subject"], prefix)
        if only:
            cards = [c for c in cards if c == only]
        if not cards:
            continue
        for card in cards:
            for flag in commit["flags"]:
                out.append({"card_id": card, "flag": flag["flag"],
                            "text": flag["text"], "commit": commit["short"]})
    return out