"""Хук git: после коммита доска сама дописывает грабли из флагов (FB-906).

Идея FB-903 — «доска сама пишет грабли в карточку» — без хука работает только
на память: забыл запустить sync, и грабля всплывёт через неделю. Хук снимает
эту забывчивость.

Правила безопасности:
- хук никогда не ломает коммит: ошибки пишутся в лог, код выхода всегда 0;
- хук ничего не коммитит: правка board.json остаётся незакоммиченной, и это
  нормально — грабля уже в карточке, её утащит следующий коммит;
- чужой хук не трогаем: если файл есть и не наш, remove молчит.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

HOOK_NAME = "post-commit"
MARK = "FossilBoard"


def hooks_dir(repo_path: str | Path) -> Path:
    return Path(repo_path).expanduser().resolve() / ".git" / "hooks"


def hook_path(repo_path: str | Path) -> Path:
    return hooks_dir(repo_path) / HOOK_NAME


def is_installed(repo_path: str | Path) -> bool:
    path = hook_path(repo_path)
    if not path.is_file():
        return False
    return MARK in path.read_text(encoding="utf-8", errors="ignore")


def board_command() -> str:
    """Путь к board.py: хук работает без PATH и из любого каталога."""
    candidate = Path(__file__).resolve().parent.parent / "board.py"
    if candidate.is_file():
        return str(candidate)
    return str(Path(sys.argv[0]).resolve())


def script_for(repo_path: str | Path, log_path: str | Path) -> str:
    return f"""#!/bin/sh
# {MARK}: после коммита доска сама дописывает грабли из флагов.
# Установлен командой board.py hook install. Снять: board.py hook remove
LOG="{log_path}"
"{sys.executable}" "{board_command()}" --repo "{Path(repo_path).resolve()}" sync >>"$LOG" 2>&1 || true
exit 0
"""


class HookError(Exception):
    """Хук поставить нельзя: там уже что-то чужое."""


def install(repo_path: str | Path, log_path: str | Path) -> Path:
    path = hook_path(repo_path)
    if path.is_file():
        existing = path.read_text(encoding="utf-8", errors="ignore")
        if MARK not in existing:
            raise HookError(
                f"там уже чужой хук: {path}\n"
                "не трогаю — переименуй его или сними вручную"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script_for(repo_path, log_path), encoding="utf-8")
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def remove(repo_path: str | Path) -> bool:
    """Снять хук. Чужой хук не трогаем — возвращаем False."""
    path = hook_path(repo_path)
    if not path.is_file():
        return False
    if MARK not in path.read_text(encoding="utf-8", errors="ignore"):
        return False
    path.unlink()
    return True
