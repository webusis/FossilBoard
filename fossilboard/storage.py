"""Хранилище: один JSON-файл на проект, атомарная запись, никаких БД.

Путь данных: <repo>/.board/board.json
Вложения:     <repo>/.board/attachments/<task-id>/<имя-файла>
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION

BOARD_DIRNAME = ".board"
BOARD_FILE = "board.json"

DEFAULT_COLUMNS = [
    {"id": "backlog", "name": "Бэклог", "wip_limit": None},
    {"id": "ready", "name": "К сессии", "wip_limit": None},
    {"id": "wip", "name": "В работе", "wip_limit": 2},
    {"id": "review", "name": "Ревью", "wip_limit": None},
    {"id": "done", "name": "Готово", "wip_limit": None},
]

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class BoardError(Exception):
    """Ошибка уровня доски: битый JSON, нет задачи, нет такого столбца."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def board_dir(repo_path: str | Path) -> Path:
    return Path(repo_path).expanduser().resolve() / BOARD_DIRNAME


def board_path(repo_path: str | Path) -> Path:
    return board_dir(repo_path) / BOARD_FILE


def tool_root() -> Path:
    """Корень установленного пакета (репозиторий FossilBoard)."""
    return Path(__file__).resolve().parent.parent


def safe_name(name: str, fallback: str = "file") -> str:
    """Имя файла без путей и управляющих символов (путь не должен вылезать)."""
    cleaned = SAFE_NAME.sub("_", Path(name).name).strip("._")
    return cleaned or fallback


def task_num(task_id: str) -> int:
    """Числовая часть ID для стабильной сортировки карточек."""
    tail = str(task_id).rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def task_sort_key(task: dict) -> tuple:
    """Стабильный порядок в колонке: по номеру ID, потом по parent_id.

    Саб-задачи идут сразу за родителем — их положение не зависит от времени
    создания, иначе любое редактирование переставляло бы diff в git.
    """
    return (task.get("parent_id") or "", task_num(task.get("id", "")), task.get("id", ""))


# --------------------------------------------------------------------------
# атомарная запись
# --------------------------------------------------------------------------

def atomic_write_json(path: str | Path, data: dict) -> None:
    """Пишем во временный файл в том же каталоге и подменяем через os.replace.

    Читатель никогда не увидит половину файла: либо старый, либо новый.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# загрузка / создание
# --------------------------------------------------------------------------

def recover_hint(repo_path: str | Path) -> str:
    return (
        f"git -C {repo_path} show HEAD:.board/{BOARD_FILE} > "
        f"{board_path(repo_path)}"
    )


def load_board(repo_path: str | Path) -> dict:
    """Прочитать доску. На битом JSON — понятная ошибка и подсказка, а не трейс."""
    path = board_path(repo_path)
    if not path.exists():
        raise BoardError(
            f"доска не инициализирована: нет {path}\n"
            f"создать:  board.py init --id <PREFIX> --name \"<имя>\" --repo {repo_path}"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BoardError(f"не читается {path}: {exc}") from exc
    if not raw.strip():
        raise BoardError(f"{path} пустой. Восстановить из git:\n  {recover_hint(repo_path)}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BoardError(
            f"{path} — битый JSON ({exc.msg}, строка {exc.lineno}, колонка {exc.colno}).\n"
            f"Ничего не потеряно, правки лежат в git. Восстановить:\n  {recover_hint(repo_path)}\n"
            f"Или откатить последний коммит доски:  git -C {repo_path} log --oneline -5 -- .board/"
        ) from exc
    if not isinstance(data, dict) or "tasks" not in data:
        raise BoardError(
            f"{path} — это не доска (нет поля 'tasks'). Восстановить:\n  {recover_hint(repo_path)}"
        )
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("counters", {}).setdefault("task", 0)
    return data


def init_board(repo: str | Path, project_id: str, name: str, task_prefix: str | None = None) -> dict:
    """Создать доску проекта. Существующую не трогает — молча перезаписывать нельзя."""
    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise BoardError(f"репозиторий не найден: {repo}")
    prefix = (task_prefix or project_id).upper()
    path = board_path(repo)
    if path.exists():
        raise BoardError(f"доска уже есть: {path}")
    data = {
        "schema_version": SCHEMA_VERSION,
        "project": {
            "id": prefix,
            "name": name,
            "repo_path": str(repo),
            "config": {"task_prefix": prefix},
            "created_at": now_iso(),
        },
        "columns": json.loads(json.dumps(DEFAULT_COLUMNS)),
        "modules": [],
        "tasks": [],
        "counters": {"task": 0},
    }
    atomic_write_json(path, data)
    (board_dir(repo) / "attachments").mkdir(parents=True, exist_ok=True)
    return data


def save_board(repo_path: str | Path, data: dict) -> None:
    atomic_write_json(board_path(repo_path), data)


# --------------------------------------------------------------------------
# поиск
# --------------------------------------------------------------------------

def find_task(data: dict, task_id: str) -> dict | None:
    for task in data["tasks"]:
        if task["id"].lower() == task_id.lower():
            return task
    return None


def get_task(data: dict, task_id: str) -> dict:
    task = find_task(data, task_id)
    if task is None:
        raise BoardError(f"задача {task_id} не найдена")
    return task


def subtasks(data: dict, parent_id: str) -> list[dict]:
    kids = [t for t in data["tasks"] if t.get("parent_id") == parent_id]
    return sorted(kids, key=task_sort_key)


def next_task_id(data: dict) -> str:
    """Номер только растёт: удалённая карточка не отдаёт свой ID обратно."""
    prefix = task_prefix(data)
    counters = data.setdefault("counters", {})
    n = int(counters.get("task", 0)) + 1
    counters["task"] = n
    # страховка от правленого вручную board.json
    for task in data["tasks"]:
        tid = task.get("id", "")
        if tid.startswith(prefix + "-"):
            n = max(n, task_num(tid))
            counters["task"] = max(counters["task"], n)
    return f"{prefix}-{n}"


def column_ids(data: dict) -> list[str]:
    return [c["id"] for c in data["columns"]]


def resolve_column(data: dict, ref: str) -> str:
    """Принять и ID колонки, и её название — хоть из CLI, хоть из UI."""
    ref = (ref or "").strip()
    for col in data["columns"]:
        if col["id"].lower() == ref.lower() or col["name"].lower() == ref.lower():
            return col["id"]
    raise BoardError(
        f"нет колонки «{ref}» (есть: {', '.join(column_ids(data))})"
    )


def wip_state(data: dict) -> dict[str, dict]:
    """Сколько карточек в колонке и не превышен ли WIP-лимит."""
    state = {}
    for col in data["columns"]:
        n = sum(1 for t in data["tasks"] if t.get("column_id") == col["id"])
        limit = col.get("wip_limit")
        state[col["id"]] = {
            "count": n,
            "wip_limit": limit,
            "over": bool(limit) and n > limit,
        }
    return state


# --------------------------------------------------------------------------
# операции над задачами
# --------------------------------------------------------------------------

def add_event(task: dict, kind: str, payload: str = "") -> None:
    task.setdefault("events", []).append({"at": now_iso(), "kind": kind, "payload": payload})


def task_prefix(data: dict) -> str:
    return (data.get("project", {}).get("config", {}).get("task_prefix")
            or data.get("project", {}).get("id") or "TASK")


def normalize_task_id(data: dict, raw: str) -> str:
    """«12» и «FB-12» → «FB-12». Префикс всегда от проекта, не от ввода."""
    prefix = task_prefix(data)
    value = (raw or "").strip().upper()
    if not value:
        raise BoardError("не указан номер карточки")
    if value.isdigit():
        return f"{prefix}-{int(value)}"          # без ведущих нулей: 007 → 7
    if not value.startswith(prefix + "-"):
        raise BoardError(f"номер должен начинаться с префикса проекта: {prefix}-N")
    tail = value[len(prefix) + 1:]
    if not tail.isdigit():
        raise BoardError(f"в номере карточки ожидаются цифры: {prefix}-N, а не «{raw}»")
    return f"{prefix}-{int(tail)}"


def reserve_task_id(data: dict, raw: str) -> str:
    """Занять конкретный номер: занять нельзя, если он уже есть.

    Счётчик подтягивается вперёд, иначе следующая автоматическая карточка
    получила бы номер уже занятой.
    """
    wanted = normalize_task_id(data, raw)
    if find_task(data, wanted) is not None:
        raise BoardError(f"карточка {wanted} уже есть")
    counters = data.setdefault("counters", {})
    counters["task"] = max(int(counters.get("task", 0)), task_num(wanted))
    return wanted


def add_task(data: dict, title: str, *, description: str = "", module_id: str | None = None,
             parent_id: str | None = None, estimate: float | None = None,
             session_label: str | None = None, column_id: str = "backlog",
             explicit_id: str | None = None) -> dict:
    title = (title or "").strip()
    if not title:
        raise BoardError("у задачи должен быть заголовок")
    if column_id not in column_ids(data):
        raise BoardError(f"нет колонки «{column_id}»")
    if parent_id:
        get_task(data, parent_id)  # родитель обязан существовать
    if module_id and not find_module(data, module_id):
        raise BoardError(f"нет модуля «{module_id}»")
    # конкретный номер проверяем и резервируем здесь же: иначе вызов в обход
    # CLI создал бы карточку K2C-77, не подвинув счётчик, и следующая была бы
    # бы с тем же номером
    wanted = reserve_task_id(data, explicit_id) if explicit_id else next_task_id(data)
    stamp = now_iso()
    task = {
        "id": wanted,
        "title": title,
        "description": description or "",
        "column_id": column_id,
        "module_id": module_id or None,
        "parent_id": parent_id or None,
        "estimate": estimate,
        "session_label": session_label or None,
        "created_at": stamp,
        "updated_at": stamp,
        "closed_at": None,
        "attachments": [],
        "events": [],
    }
    add_event(task, "created", f"из «{column_name(data, column_id)}»")
    data["tasks"].append(task)
    return task


def column_name(data: dict, column_id: str) -> str:
    for col in data["columns"]:
        if col["id"] == column_id:
            return col["name"]
    return column_id


def update_task(data: dict, task_id: str, changes: dict) -> dict:
    task = get_task(data, task_id)
    allowed = {
        "title", "description", "column_id", "module_id", "parent_id",
        "estimate", "session_label",
    }
    applied = []
    for key, value in changes.items():
        if key not in allowed:
            continue
        if key == "column_id":
            continue
        if key == "title":
            value = (value or "").strip()
            if not value:
                raise BoardError("заголовок не может быть пустым")
        if key == "module_id" and value and not find_module(data, value):
            raise BoardError(f"нет модуля «{value}»")
        if key == "parent_id" and value:
            if value == task["id"]:
                raise BoardError("задача не может быть родителем самой себя")
            get_task(data, value)
        if task.get(key) != value:
            task[key] = value if value not in ("",) else (None if key != "description" else "")
            applied.append(key)
    if "column_id" in changes and changes["column_id"]:
        move_task(data, task_id, changes["column_id"])
    if applied:
        task["updated_at"] = now_iso()
        add_event(task, "edited", ", ".join(sorted(applied)))
    return task


def move_task(data: dict, task_id: str, column_id: str) -> dict:
    task = get_task(data, task_id)
    target = resolve_column(data, column_id)
    old = task.get("column_id")
    if old == target:
        return task
    task["column_id"] = target
    task["updated_at"] = now_iso()
    if target == "done":
        task["closed_at"] = now_iso()
        add_event(task, "closed", "")
    else:
        task["closed_at"] = None
        add_event(task, "moved", f"{old} → {target}")
    return task


def delete_task(data: dict, task_id: str, with_children: bool = True) -> list[str]:
    """Удалить задачу. ID не переиспользуется (счётчик в counters)."""
    task = get_task(data, task_id)
    removed = [task["id"]]
    if with_children:
        for child in subtasks(data, task["id"]):
            removed += delete_task(data, child["id"], with_children=True)
    data["tasks"] = [t for t in data["tasks"] if t["id"] not in removed]
    return removed


# --------------------------------------------------------------------------
# модули
# --------------------------------------------------------------------------

def find_module(data: dict, ref: str) -> dict | None:
    ref = (ref or "").strip().lower()
    for mod in data["modules"]:
        if mod["id"].lower() == ref or mod["name"].lower() == ref:
            return mod
    return None


def add_module(data: dict, name: str, color: str = "#88ccee", description: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise BoardError("у модуля должно быть имя")
    if find_module(data, name):
        raise BoardError(f"модуль «{name}» уже есть")
    mod = {"id": uuid.uuid4().hex[:8], "name": name, "color": color or "#88ccee",
           "description": description or ""}
    data["modules"].append(mod)
    return mod


def update_module(data: dict, ref: str, changes: dict) -> dict:
    mod = find_module(data, ref)
    if mod is None:
        raise BoardError(f"модуль «{ref}» не найден")
    for key in ("name", "color", "description"):
        if key in changes and changes[key] is not None:
            if key == "name":
                changes[key] = changes[key].strip()
                if not changes[key]:
                    raise BoardError("имя модуля не может быть пустым")
                other = find_module(data, changes[key])
                if other and other["id"] != mod["id"]:
                    raise BoardError(f"модуль «{changes[key]}» уже есть")
            mod[key] = changes[key]
    return mod


def delete_module(data: dict, ref: str) -> dict:
    mod = find_module(data, ref)
    if mod is None:
        raise BoardError(f"модуль «{ref}» не найден")
    for task in data["tasks"]:
        if task.get("module_id") == mod["id"]:
            task["module_id"] = None
    data["modules"] = [m for m in data["modules"] if m["id"] != mod["id"]]
    return mod


# --------------------------------------------------------------------------
# вложения
# --------------------------------------------------------------------------

def attachment_dir(repo_path: str | Path, task_id: str) -> Path:
    return board_dir(repo_path) / "attachments" / task_id


def attach_file(repo_path: str | Path, task_id: str, src: str | Path, name: str | None = None) -> dict:
    """Скопировать файл в .board/attachments/<task-id>/ и внести в карточку."""
    data = load_board(repo_path)
    task = get_task(data, task_id)
    src = Path(src).expanduser()
    if not src.is_file():
        raise BoardError(f"файл не найден: {src}")
    fname = safe_name(name or src.name)
    target_dir = attachment_dir(repo_path, task["id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / fname
    if target.exists() or target.is_symlink():
        target.unlink()
    shutil.copy2(src, target)
    rel = f"attachments/{task['id']}/{fname}"
    item = {"name": fname, "path": rel, "size": target.stat().st_size, "added_at": now_iso()}
    task["attachments"] = [a for a in task.get("attachments", []) if a["name"] != fname]
    task["attachments"].append(item)
    task["attachments"].sort(key=lambda a: a["name"])
    task["updated_at"] = now_iso()
    add_event(task, "attach", fname)
    save_board(repo_path, data)
    return item


def delete_attachment(repo_path: str | Path, task_id: str, name: str) -> None:
    data = load_board(repo_path)
    task = get_task(data, task_id)
    fname = safe_name(name)
    task["attachments"] = [a for a in task.get("attachments", []) if a["name"] != fname]
    path = attachment_dir(repo_path, task["id"]) / fname
    if path.exists():
        path.unlink()
    task["updated_at"] = now_iso()
    add_event(task, "detach", fname)
    save_board(repo_path, data)


IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
}


def guess_content_type(name: str) -> str:
    """Картинки — по своей таблице (надёжнее mimetypes для svg/ico),
    остальное — через mimetypes, иначе бинарник."""
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_TYPES:
        return IMAGE_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"