"""Создание и подключение репозиториев из интерфейса.

Два сценария, оба с ручным вводом пути:
  * создать новую репу: папка + git init + .board/board.json;
  * открыть существующую: регистрируем в конфиге, доску не трогаем.

Ограничения намеренные: путь должен быть абсолютным, никаких команд из
пользовательского ввода не выполняется, git init вызывается только для
создаваемой папки. Это локальный инструмент для одного человека, но лишних
возможностей тут не нужно.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import config as cfgmod
from .storage import BoardError, board_path, init_board, load_board, save_board

MAX_SUBMIT_PROJECTS = 100


class RepoError(Exception):
    """Не получилось создать или подключить репозиторий."""


def expand(path: str) -> Path:
    raw = (path or "").strip()
    if not raw:
        raise RepoError("путь не указан")
    if raw.startswith("~"):
        raw = str(Path.home()) + raw[1:]
    result = Path(raw).expanduser()
    if not result.is_absolute():
        raise RepoError(f"нужен абсолютный путь, а не «{raw}»: относительные не предсказуемы")
    return Path(os.path.normpath(str(result)))


def suggest_id(path: Path, taken: list[str]) -> str:
    """Имя каталога как префикс карточек: k2c-proto → K2C."""
    name = re.sub(r"[^A-Za-z0-9]+", "", path.name) or "TASK"
    prefix = name[:4].upper()
    if prefix in {t.upper() for t in taken}:
        digits = re.sub(r"\D", "", path.name)
        if digits:
            prefix = (prefix + digits[-2:])[:6]
        while prefix in {t.upper() for t in taken}:
            prefix = prefix + "X"
    return prefix


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def git_init(path: Path) -> None:
    try:
        proc = subprocess.run(["git", "init", "-q"], cwd=str(path),
                              capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError as exc:
        raise RepoError("git не найден в PATH — репу создать не получится") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoError("git init завис") from exc
    if proc.returncode != 0:
        raise RepoError(f"git init не сработал: {proc.stderr.strip() or proc.returncode}")


def describe(path: Path) -> dict:
    """Что сейчас по этому пути — UI показывает это до кнопки «создать»."""
    info = {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "is_git": is_git_repo(path) if path.is_dir() else False,
        "has_board": board_path(path).is_file() if path.is_dir() else False,
    }
    info["board_ok"] = True
    if info["has_board"]:
        try:
            info["board_ok"] = isinstance(load_board(path).get("tasks"), list)
        except BoardError as exc:
            info["board_ok"] = False
            info["board_error"] = str(exc).splitlines()[0]
    return info


NOISE_DIRS = {"__pycache__", "node_modules", "venv", "build", "dist", "target",
              "vendor", "__snapshots__", ".mypy_cache", ".pytest_cache"}


def browse(path: str | None = None) -> list[dict]:
    """Подкаталоги для подсказки при вводе пути руками."""
    raw = (path or "").strip()
    base = Path(str(Path.home()) + raw[1:]) if raw.startswith("~") else Path(raw or Path.home())
    if not base.is_absolute():
        base = Path.home()
    if base.is_file():
        base = base.parent
    if not base.is_dir():
        base = base.parent if base.parent.is_dir() else Path.home()
    out = []
    try:
        for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith(".") or child.name in NOISE_DIRS:
                continue
            if child.is_dir():
                out.append({"name": child.name, "path": str(child), "git": is_git_repo(child),
                            "board": board_path(child).is_file()})
    except OSError as exc:
        raise RepoError(f"не читается {base}: {exc}") from exc
    return [{"parent": str(base), "dirs": out[:MAX_SUBMIT_PROJECTS]}]


def attach(cfg: dict, path_str: str, project_id: str | None = None,
           name: str | None = None, create: bool = False) -> dict:
    """Открыть существующую репу или создать новую. Возвращает проект для UI."""
    path = expand(path_str)
    if path.exists() and not path.is_dir():
        raise RepoError(f"{path} — это файл, а не каталог")
    if create:
        if path.exists() and any(path.iterdir()):
            # не стираем чужое: досоздаём только недостающее
            missing = []
            if not is_git_repo(path):
                missing.append("git init")
            if not board_path(path).is_file():
                missing.append("доска")
            if not missing:
                raise RepoError(f"{path} уже есть и готова — откройте её вместо создания")
            if not is_git_repo(path):
                git_init(path)
        else:
            path.mkdir(parents=True, exist_ok=True)
            git_init(path)
        if not board_path(path).is_file():
            prefix = (project_id or suggest_id(path, [p["id"] for p in cfg.get("projects", [])]))
            init_board(path, prefix, name or path.name, task_prefix=prefix)
    else:
        if not path.is_dir():
            raise RepoError(f"нет такого каталога: {path}")
        if not is_git_repo(path):
            raise RepoError(f"{path} — не git-репозиторий (нет .git)")

    if board_path(path).is_file():
        data = load_board(path)
        project_id = data["project"]["id"]
        name = data["project"]["name"]
        repo_now = str(path)
        if data["project"].get("repo_path") != repo_now:
            data["project"]["repo_path"] = repo_now
            save_board(path, data)
    elif create:
        raise RepoError(f"доска не создалась: {board_path(path)}")
    else:
        raise RepoError(
            f"в {path} нет доски (.board/board.json).\n"
            f"Создать её:  board.py init --id <PREFIX> --name \"<имя>\" --repo {path}"
        )
    proj = cfgmod.upsert_project(cfg, project_id or suggest_id(path, []), name or path.name,
                                 str(path))
    cfgmod.save(cfg)
    return {"project": proj, "info": describe(path)}