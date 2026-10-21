"""Конфигурация FossilBoard: пути, проекты, параметры сервера.

Шаблон лежит в репозитории рядом с board.py — `config.example.json`.
Рабочий конфиг ищется в таком порядке:

    1. $FOSSILBOARD_CONFIG               — прямой путь к файлу
    2. ./config.json                     — рядом с доской, в репозитории проекта
    3. ~/.config/fossilboard/config.json — общесистемный, для всех досок сразу
    4. значения по умолчанию

Шаблон объясняет каждое поле, а рабочий конфиг кладётся в репозиторий проекта:
доска одна, настроек немного, и ехать им лучше вместе с проектом.

Приоритеты поверх любого файла (по возрастанию):
    флаг CLI → переменная окружения → конфиг → значения по умолчанию

Переменные окружения:
    FOSSILBOARD_CONFIG   другой путь к конфигу
    FOSSILBOARD_REPO     репозиторий по умолчанию (для --repo)
    FOSSILBOARD_PROJECT  проект по умолчанию (для --project)
    FOSSILBOARD_PORT     порт сервера
    FOSSILBOARD_HOST     адрес прослушивания
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

CONFIG_VERSION = 1
APP_NAME = "fossilboard"
TEMPLATE_NAME = "config.example.json"
LOCAL_NAME = "config.json"

DEFAULTS = {
    "version": CONFIG_VERSION,
    "server": {
        "host": "0.0.0.0",
        "port": 8770,
    },
    "attachments": {
        "max_upload_mb": 64,
    },
    "git": {
        "diff_max_lines": 4000,
    },
    "ui": {
        "theme": "dark",          # только тёмная: переключателя нет и не планируется
        "default_session": None,  # метка сессии для новых карточек, напр. "вечер"
    },
    "paths": {
        # неотсортированные скриншоты и черновики — сюда, а не в дом и не в репу
        "scratch": "/tmp/fossilboard",
        "logs": "~/.local/state/fossilboard/logs",
    },
    "default_project": None,   # id проекта без --project: board.py config set default_project FB
    "flags": {                  # флаги-грабли в сообщениях коммитов (FB-903)
        "rake": "грабля или урок: что не сработало и почему",
        "gotcha": "неочевидное поведение, о котором стоит знать",
        "fix": "как починили",
        "todo": "долг, который остался",
    },
    "projects": [],
}


class ConfigError(Exception):
    pass


def user_config_path() -> Path:
    """Общесистемный конфиг: ~/.config/fossilboard/config.json."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(xdg).expanduser() / APP_NAME / LOCAL_NAME


def local_config_path(start: str | Path | None = None) -> Path:
    """Конфиг рядом с доской: ./config.json в корне репозитория."""
    return Path(start or Path.cwd()) / LOCAL_NAME


def template_path() -> Path:
    """Шаблон из репозитория FossilBoard — лежит рядом с board.py."""
    try:
        from .storage import tool_root
        return tool_root() / TEMPLATE_NAME
    except Exception:  # pragma: no cover
        return Path(__file__).resolve().parent.parent / TEMPLATE_NAME


def config_path(start: str | Path | None = None) -> Path:
    """Куда писать конфиг: путь из окружения → локальный → общесистемный.

    Возвращает первый существующий; если не существует ни один — локальный,
    потому что конфиг логично коммитить вместе с проектом.
    """
    override = os.environ.get("FOSSILBOARD_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    local = local_config_path(start)
    if local.is_file():
        return local.resolve()
    home = user_config_path()
    if home.is_file():
        return home
    return local


def candidate_paths(start: str | Path | None = None) -> list[Path]:
    """Все места, откуда читается конфиг, — для сообщений пользователю."""
    out = []
    if os.environ.get("FOSSILBOARD_CONFIG"):
        out.append(Path(os.environ["FOSSILBOARD_CONFIG"]).expanduser())
    out.append(local_config_path(start))
    out.append(user_config_path())
    return out


def _deep_merge(base: dict, override: dict) -> dict:
    # deepcopy обязателен: DEFAULTS содержит списки, и поверхностная копия
    # приводила к тому, что upsert_project мутировал общий список умолчаний,
    # а проекты «появлялись» у всех следующих вызовов подряд
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key.startswith("_"):       # комментарии из шаблона в данные не превращаем
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _expand(value):
    return Path(value).expanduser() if isinstance(value, str) and value.startswith("~") else value


def load(path: str | Path | None = None) -> dict:
    """Прочитать конфиг и наложить его на значения по умолчанию."""
    cfg_path = Path(path).expanduser() if path else config_path()
    data = defaults()
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{cfg_path} — битый JSON ({exc.msg}, строка {exc.lineno}).\n"
                f"Исправить вручную или удалить файл: доска продолжит работать "
                f"на значениях по умолчанию."
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{cfg_path} — ожидался объект JSON")
        data = _deep_merge(data, raw)
    data["_path"] = str(cfg_path)
    data["_exists"] = cfg_path.exists()
    return data


def defaults(path: str | Path | None = None) -> dict:
    """Свежая копия значений по умолчанию — мутабельные вложенные списки опасны."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg["_path"] = str(Path(path).expanduser() if path else config_path())
    cfg["_exists"] = Path(cfg["_path"]).exists()
    return cfg


def save(cfg: dict, path: str | Path | None = None) -> Path:
    """Записать конфиг. Ключи, начинающиеся с подчёркивания, — служебные."""
    cfg_path = Path(path).expanduser() if path else Path(cfg.get("_path") or config_path())
    payload = {k: v for k, v in cfg.items() if not k.startswith("_")}
    payload["version"] = CONFIG_VERSION
    from .storage import atomic_write_json
    atomic_write_json(cfg_path, payload)
    return cfg_path


def get(cfg: dict, dotted: str, default=None):
    """cfg_get(cfg, 'server.port') — доступ по точечному пути."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_value(cfg: dict, dotted: str, value) -> dict:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return cfg


def cast(value: str):
    """Привести строку из CLI к типу: json для неё, иначе как есть."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def effective_server(cfg: dict, host: str | None = None, port: int | None = None) -> tuple[str, int]:
    """Адрес и порт: флаг CLI → переменная окружения → конфиг → умолчание."""
    env_port = os.environ.get("FOSSILBOARD_PORT")
    env_host = os.environ.get("FOSSILBOARD_HOST")
    final_host = host or env_host or get(cfg, "server.host", "0.0.0.0")
    final_port = port or (int(env_port) if env_port else get(cfg, "server.port", 8770))
    return final_host, int(final_port)


def flags_registry(cfg: dict | None) -> dict:
    """Реестр флагов-граблей: умолчания + то, что добавил пользователь.

    Расширяется конфигом машины без правки кода:
        "flags": {"мой-флаг": "что это значит"}
    """
    base = dict(DEFAULTS.get("flags", {}))
    extra = (cfg or {}).get("flags") or {}
    if isinstance(extra, dict):
        for name, desc in extra.items():
            key = str(name).strip().lstrip("#")
            if key:
                base[key] = desc or ""
    return base


def project(cfg: dict, ref: str | None) -> dict | None:
    """Найти проект по id (без учёта регистра)."""
    if not ref:
        return None
    for proj in cfg.get("projects", []):
        if str(proj.get("id", "")).lower() == ref.lower():
            return proj
    return None


def default_project(cfg: dict) -> dict | None:
    """Единственный проект в конфиге — значит, очевидный выбор."""
    projects = cfg.get("projects", [])
    return projects[0] if len(projects) == 1 else None


def resolve_repo(cfg: dict, repo: str | None = None, ref: str | None = None) -> str | None:
    """--repo → FOSSILBOARD_REPO → --project/FOSSILBOARD_PROJECT →
    default_project из конфига → единственный проект."""
    if repo:
        return str(Path(repo).expanduser().resolve())
    env_repo = os.environ.get("FOSSILBOARD_REPO")
    if env_repo:
        return str(Path(env_repo).expanduser().resolve())
    proj = project(cfg, ref or os.environ.get("FOSSILBOARD_PROJECT"))
    if proj:
        return str(Path(proj["repo_path"]).expanduser().resolve())
    proj = project(cfg, cfg.get("default_project"))
    if proj:
        return str(Path(proj["repo_path"]).expanduser().resolve())
    only = default_project(cfg)
    if only:
        return str(Path(only["repo_path"]).expanduser().resolve())
    return None


def upsert_project(cfg: dict, project_id: str, name: str, repo_path: str) -> dict:
    """Добавить или обновить проект. Путь сохраняем развёрнутым."""
    resolved = str(Path(repo_path).expanduser().resolve())
    projects = cfg.setdefault("projects", [])
    for proj in projects:
        if str(proj.get("id", "")).lower() == project_id.lower():
            proj.update({"name": name, "repo_path": resolved})
            return proj
    entry = {"id": project_id.upper(), "name": name, "repo_path": resolved}
    projects.append(entry)
    return entry


def remove_project(cfg: dict, project_id: str) -> bool:
    projects = cfg.get("projects", [])
    before = len(projects)
    cfg["projects"] = [p for p in projects if str(p.get("id", "")).lower() != project_id.lower()]
    return len(cfg["projects"]) != before


def legacy_registries(candidates=None) -> list[Path]:
    """Где искать старый реестр из репозитория: текущий каталог и корень доски."""
    if candidates is not None:
        return [Path(c) for c in candidates]
    roots = [Path.cwd()]
    try:
        from .storage import tool_root
        roots.append(tool_root())
    except Exception:  # pragma: no cover - конфиг должен работать и без storage
        pass
    seen, out = set(), []
    for root in roots:
        path = root / ".board" / "projects.json"
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def migrate_legacy_registry(cfg: dict, cfg_path: str | Path | None = None,
                            candidates=None) -> list[str]:
    """Перенести старый .board/projects.json из репозитория в конфиг.

    Нужно один раз: до конфигов реестр жил в репе и тащил за собой абсолютные
    пути машины. Возвращает список перенесённых проектов.
    """
    path = Path(cfg_path).expanduser() if cfg_path else config_path()
    if path.exists():
        return []
    moved: list[str] = []
    for legacy in legacy_registries(candidates):
        if not legacy.is_file():
            continue
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for proj in data.get("projects", []):
            if not project(cfg, proj.get("id", "")) and proj.get("repo_path"):
                upsert_project(cfg, proj["id"], proj.get("name", proj["id"]), proj["repo_path"])
                moved.append(proj["id"])
        legacy.unlink()  # реестр больше не живёт в репозитории
    return moved