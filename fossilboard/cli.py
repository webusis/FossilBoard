"""CLI: доска должна работать без браузера — из терминала и из агента."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import gitlink
from .storage import (
    BoardError, add_module, add_task, attach_file, board_dir, board_path, column_name,
    delete_attachment, delete_module, delete_task, find_module, find_task, init_board,
    load_board, move_task, resolve_column, save_board, subtasks, update_module,
    update_task, wip_state,
)

EXIT_OK = 0
EXIT_ERR = 1


def die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return EXIT_ERR


def repo_dir_size(path: Path) -> str:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return human_size(total)


def human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------
# выбор проекта
# --------------------------------------------------------------------------

def get_config() -> dict:
    """Конфиг машины; при первом запуске подхватываем старый реестр из репы."""
    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as exc:
        print(f"предупреждение: {exc}", file=sys.stderr)
        cfg = cfgmod.defaults()  # работаем на умолчаниях, но не молчим об ошибке
    moved = cfgmod.migrate_legacy_registry(cfg, cfg.get("_path"))
    if moved:
        cfgmod.save(cfg)
        print(f"проекты перенесены из .board/projects.json в конфиг: {', '.join(moved)}")
    return cfg


def resolve_repo(args: argparse.Namespace) -> str | None:
    cfg = get_config()
    repo = cfgmod.resolve_repo(cfg, getattr(args, "repo", None),
                               getattr(args, "project", None))
    if repo:
        return repo
    projects = cfg.get("projects", [])
    if projects:
        ids = ", ".join(p["id"] for p in projects)
        raise BoardError(f"проект не указан, в конфиге их несколько ({ids}) — добавь --project")
    raise BoardError(
        "в конфиге нет проектов. Создать доску:\n"
        "  board.py init --id FB --name \"FossilBoard\" --repo <путь>"
    )


def load(args: argparse.Namespace) -> tuple[str, dict]:
    repo = resolve_repo(args)
    if not repo:
        raise BoardError(f"проект не найден в конфиге: --project {getattr(args, 'project', None)}")
    return repo, load_board(repo)


# --------------------------------------------------------------------------
# отрисовка карточки в терминал
# --------------------------------------------------------------------------

def fmt_task(task: dict, data: dict, *, indent: int = 0) -> list[str]:
    pad = " " * indent
    lines = [f"{pad}{task['id']}  {task['title']}"]
    meta = [f"колонка: {column_name(data, task['column_id'])}"]
    if task.get("module_id"):
        mod = find_module(data, task["module_id"])
        meta.append("модуль: " + (mod["name"] if mod else str(task["module_id"])))
    if task.get("estimate"):
        meta.append(f"оценка: {task['estimate']} ч")
    if task.get("session_label"):
        meta.append(f"сессия: {task['session_label']}")
    if task.get("parent_id"):
        meta.append(f"родитель: {task['parent_id']}")
    kids = subtasks(data, task["id"])
    if kids:
        done = sum(1 for k in kids if k["column_id"] == "done")
        meta.append(f"саб-задачи: {done} / {len(kids)} готово")
    if task.get("attachments"):
        meta.append("вложения: " + ", ".join(a["name"] for a in task["attachments"]))
    lines.append(pad + "  " + "  ·  ".join(meta))
    if task.get("description"):
        for raw in task["description"].splitlines():
            lines.append(pad + "  | " + raw)
    if task.get("events"):
        lines.append(pad + "  события:")
        for ev in task["events"][-5:]:
            lines.append(pad + f"    {ev['at'][:19]}  {ev['kind']}  {ev['payload']}".rstrip())
    for kid in kids:
        lines += fmt_task(kid, data, indent=indent + 4)
    return lines


# --------------------------------------------------------------------------
# команды
# --------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    path = board_path(args.repo)
    existed = path.exists()
    data = load_board(args.repo) if existed else init_board(args.repo, args.id, args.name,
                                                           task_prefix=args.task_prefix)
    # повторный init после git clone: данные не трогаем, но путь к репе
    # переписываем — иначе клонированная доска смотрит в чужой каталог
    cfg = get_config()
    proj = cfgmod.upsert_project(cfg, data["project"]["id"], data["project"]["name"],
                                 data["project"]["repo_path"])
    cfgmod.save(cfg)
    if existed:
        # путь к репе записан в самой доске — после клона он чужой, обновляем
        repo_now = str(Path(args.repo).expanduser().resolve())
        if data["project"].get("repo_path") != repo_now:
            data["project"]["repo_path"] = repo_now
            save_board(args.repo, data)
        if args.id and args.id.upper() != data["project"]["id"]:
            print(f"внимание: доска уже настроена на префикс {data['project']['id']}, "
                  f"а в команде указан {args.id.upper()} — оставляю {data['project']['id']}",
                  file=sys.stderr)
        print(f"доска уже была: {path}")
        print(f"данные не тронуты, репозиторий перерегистрирован: {repo_now}")
    else:
        print(f"доска создана: {path}")
        print(f"проект: {proj['id']} «{proj['name']}» → {proj['repo_path']}")
    return EXIT_OK


def cmd_projects(args: argparse.Namespace) -> int:
    cfg = get_config()
    if not cfg["projects"]:
        print("в конфиге нет проектов. Создать доску:  "
              "board.py init --id FB --name \"FossilBoard\" --repo <путь>")
        return EXIT_OK
    for proj in cfg["projects"]:
        board = Path(proj["repo_path"]) / ".board" / "board.json"
        mark = "✓" if board.is_file() else "нет доски"
        print(f"{proj['id']:<6} {proj['name']:<16} {proj['repo_path']}  [{mark}]")
    print(f"\nконфиг: {cfg['_path']}")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """Показать, создать или поправить конфиг машины."""
    cfg = get_config()
    if args.config_cmd == "path":
        print(cfg["_path"])
        return EXIT_OK
    if args.config_cmd == "init":
        path = cfgmod.save(cfg)
        print(f"конфиг создан: {path}")
        return EXIT_OK
    if args.config_cmd == "set":
        if not args.key:
            return die("нужен ключ: board.py config set server.port 9000")
        value = cfgmod.cast(args.value)
        old = cfgmod.get(cfg, args.key)
        cfgmod.set_value(cfg, args.key, value)
        cfgmod.save(cfg)
        print(f"{args.key}: {old!r} → {value!r}")
        return EXIT_OK
    if args.config_cmd == "add-project":
        if not (args.id and args.repo):
            return die("нужно: board.py config add-project FB \"FossilBoard\" --repo <путь>")
        proj = cfgmod.upsert_project(cfg, args.id, args.name or args.id, args.repo)
        cfgmod.save(cfg)
        print(f"{proj['id']}  {proj['name']}  →  {proj['repo_path']}")
        print(f"конфиг: {cfg['_path']}")
        return EXIT_OK
    if args.config_cmd == "rm-project":
        if not args.id:
            return die("нужен id: board.py config rm-project FB")
        if not cfgmod.remove_project(cfg, args.id):
            return die(f"проект {args.id} в конфиге не найден")
        cfgmod.save(cfg)
        print(f"проект {args.id} убран из конфига (доска в репе не тронута)")
        return EXIT_OK
    # показать
    shown = {k: v for k, v in cfg.items() if not k.startswith("_")}
    print(f"конфиг: {cfg['_path']}{'' if cfg['_exists'] else '  (создан из значений по умолчанию)'}")
    print(json.dumps(shown, indent=2, sort_keys=True, ensure_ascii=False))
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    _, data = load(args)
    mod = find_module(data, args.module) if args.module else None
    if args.module and mod is None:
        return die(f"модуль «{args.module}» не найден")
    column = resolve_column(data, args.column) if args.column else None
    needle = (args.search or "").lower().strip()
    rows = []
    for task in data["tasks"]:
        if args.open and task["column_id"] == "done":
            continue
        if mod and task.get("module_id") != mod["id"]:
            continue
        if column and task["column_id"] != column:
            continue
        if args.session and task.get("session_label") != args.session:
            continue
        if args.parents and not subtasks(data, task["id"]):
            continue
        hay = (task["title"] + " " + (task.get("description") or "")).lower()
        if needle and needle not in hay:
            continue
        rows.append(task)
    rows.sort(key=lambda t: (t.get("parent_id") or "", int(t["id"].rsplit("-", 1)[-1])))
    if not rows:
        print("пусто")
        return EXIT_OK
    for task in rows:
        kids = subtasks(data, task["id"])
        mark = "◼" if kids else " "
        est = f"  ~{task['estimate']}ч" if task.get("estimate") else ""
        ses = f"  [{task['session_label']}]" if task.get("session_label") else ""
        print(f"{mark} {task['id']:<10} {column_name(data, task['column_id']):<12} "
              f"{task['title']}{est}{ses}")
        if args.verbose:
            for line in fmt_task(task, data, indent=2)[1:]:
                print(line)
    return EXIT_OK


def cmd_add(args: argparse.Namespace) -> int:
    repo, data = load(args)
    module_id = None
    if args.module:
        mod = find_module(data, args.module)
        if mod is None:
            return die(f"модуль «{args.module}» не найден (создай: board.py module add \"{args.module}\")")
        module_id = mod["id"]
    task = add_task(data, args.title, description=args.desc or "", module_id=module_id,
                    parent_id=args.parent, estimate=args.estimate,
                    session_label=args.session, column_id=args.column or "backlog")
    save_board(repo, data)
    print(task["id"])
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    repo, data = load(args)
    task = find_task(data, args.id)
    if task is None:
        return die(f"задача {args.id} не найдена")
    print("\n".join(fmt_task(task, data)))
    print("\nпривязанные коммиты:")
    try:
        commits = gitlink.commits_for_task(repo, task["id"])
    except gitlink.GitError as exc:
        commits = []
        print(f"  (git недоступен: {exc})")
    if not commits:
        print(f"  нет. Сделай коммит так:")
        print(f"    git commit -m '{task['id']}: описание'")
    for c in commits:
        print(f"  {c['short']}  {c['date'][:10]}  {c['author']:<14}  {c['subject']}")
    if args.diff and commits:
        print("\n" + "=" * 60)
        print(gitlink.show_commit(repo, commits[0]["hash"])["text"])
    return EXIT_OK


def cmd_move(args: argparse.Namespace) -> int:
    repo, data = load(args)
    move_task(data, args.id, args.column)
    save_board(repo, data)
    task = find_task(data, args.id)
    print(f"{task['id']} → {column_name(data, task['column_id'])}")
    wip = wip_state(data)
    state = wip.get(task["column_id"], {})
    if state.get("over"):
        print(f"внимание: WIP-лимит колонки превышен ({state['count']} > {state['wip_limit']})",
              file=sys.stderr)
    return EXIT_OK


def cmd_done(args: argparse.Namespace) -> int:
    repo, data = load(args)
    move_task(data, args.id, "done")
    save_board(repo, data)
    print(f"{args.id} ✓ закрыта")
    return EXIT_OK


def cmd_edit(args: argparse.Namespace) -> int:
    repo, data = load(args)
    changes = {}
    if args.title is not None:
        changes["title"] = args.title
    if args.desc is not None:
        changes["description"] = args.desc
    if args.module is not None:
        if args.module in ("", "none", "null"):
            changes["module_id"] = None
        else:
            mod = find_module(data, args.module)
            if mod is None:
                return die(f"модуль «{args.module}» не найден")
            changes["module_id"] = mod["id"]
    if args.estimate is not None:
        changes["estimate"] = args.estimate
    if args.session is not None:
        changes["session_label"] = args.session or None
    if args.parent is not None:
        changes["parent_id"] = args.parent or None
    if not changes:
        return die("нечего менять: задай --title/--desc/--module/--estimate/--session/--parent")
    task = update_task(data, args.id, changes)
    save_board(repo, data)
    print(task["id"] + " обновлена")
    return EXIT_OK


def cmd_rm(args: argparse.Namespace) -> int:
    repo, data = load(args)
    task = find_task(data, args.id)
    if task is None:
        return die(f"задача {args.id} не найдена")
    if not args.yes:
        answer = input(f"удалить {task['id']} «{task['title']}»? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "д", "да"):
            print("отмена")
            return EXIT_OK
    removed = delete_task(data, args.id)
    save_board(repo, data)
    print("удалено: " + ", ".join(removed))
    return EXIT_OK


def cmd_attach(args: argparse.Namespace) -> int:
    repo, data = load(args)
    item = attach_file(repo, args.id, args.path, name=args.name)
    print(f"{args.id} ← {item['name']}  {human_size(item['size'])}  →  .board/{item['path']}")
    return EXIT_OK


def cmd_detach(args: argparse.Namespace) -> int:
    repo, _ = load(args)
    delete_attachment(repo, args.id, args.name)
    print(f"{args.id} − {args.name}")
    return EXIT_OK


def cmd_module(args: argparse.Namespace) -> int:
    repo, data = load(args)
    if args.module_cmd == "add":
        mod = add_module(data, args.name, color=args.color, description=args.desc or "")
        save_board(repo, data)
        print(f"{mod['id']}  {mod['name']}  {mod['color']}")
        return EXIT_OK
    if args.module_cmd == "list":
        if not data["modules"]:
            print("модулей нет")
            return EXIT_OK
        for mod in data["modules"]:
            count = sum(1 for t in data["tasks"] if t.get("module_id") == mod["id"])
            print(f"{mod['id']:<10} {mod['name']:<14} {mod['color']:<9} карточек: {count}")
        return EXIT_OK
    if args.module_cmd == "rm":
        mod = delete_module(data, args.name)
        save_board(repo, data)
        print(f"модуль «{mod['name']}» удалён, карточки остались без модуля")
        return EXIT_OK
    if args.module_cmd in ("rename", "color", "edit"):
        changes = {}
        if args.module_cmd == "rename":
            changes["name"] = args.new_name or args.name
        if args.module_cmd == "color":
            changes["color"] = args.new_name
        if args.module_cmd == "edit":
            if args.new_name:
                changes["name"] = args.new_name
            if args.color:
                changes["color"] = args.color
            if args.desc is not None:
                changes["description"] = args.desc
        mod = update_module(data, args.name, changes)
        save_board(repo, data)
        print(f"{mod['id']}  {mod['name']}  {mod['color']}")
        return EXIT_OK
    return die("неизвестная подкоманда модуля")


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import ServeError, serve
    cfg = get_config()
    repo = resolve_repo(args)
    host, port = cfgmod.effective_server(cfg, args.host, args.port)
    try:
        return serve(repo=repo, host=host, port=port, cfg=cfg)
    except ServeError as exc:
        return die(str(exc))


def cmd_json(args: argparse.Namespace) -> int:
    """Сырой JSON доски — удобно для агента и для jq."""
    repo, data = load(args)
    print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
    return EXIT_OK


# --------------------------------------------------------------------------
# парсер
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="board.py", description="FossilBoard — локальная kanban-доска")
    p.add_argument("--version", action="version", version=f"FossilBoard {__version__}")
    p.add_argument("--repo", help="путь к репе проекта (перекрывает реестр)")
    p.add_argument("--project", "-p", help="id проекта из реестра")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="создать доску проекта")
    s.add_argument("--id", required=True, help="префикс ID, напр. FB")
    s.add_argument("--name", required=True)
    s.add_argument("--repo", required=True)
    s.add_argument("--task-prefix", help="префикс задач, по умолчанию --id")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("projects", help="проекты из конфига")
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("config", help="конфиг машины: пути, проекты, порт")
    csub = s.add_subparsers(dest="config_cmd")
    cs = csub.add_parser("path", help="где лежит конфиг")
    cs.set_defaults(func=cmd_config)
    cs = csub.add_parser("init", help="создать конфиг со значениями по умолчанию")
    cs.set_defaults(func=cmd_config)
    cs = csub.add_parser("set", help="изменить параметр: server.port 9000")
    cs.add_argument("key")
    cs.add_argument("value", nargs="?", default="")
    cs.set_defaults(func=cmd_config)
    cs = csub.add_parser("add-project", help="добавить проект в конфиг")
    cs.add_argument("id")
    cs.add_argument("name", nargs="?")
    cs.add_argument("--repo", required=True)
    cs.set_defaults(func=cmd_config)
    cs = csub.add_parser("rm-project", help="убрать проект из конфига")
    cs.add_argument("id")
    cs.set_defaults(func=cmd_config)
    s.set_defaults(func=cmd_config, config_cmd="show")

    s = sub.add_parser("list", help="карточки")
    s.add_argument("--module", "-m")
    s.add_argument("--column", "-c")
    s.add_argument("--session")
    s.add_argument("--parents", action="store_true", help="только с саб-задачами")
    s.add_argument("--open", action="store_true", help="скрыть закрытые")
    s.add_argument("--search", "-s")
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("add", help="создать карточку")
    s.add_argument("title")
    s.add_argument("-m", "--module")
    s.add_argument("-d", "--desc", help="описание (markdown)")
    s.add_argument("--parent")
    s.add_argument("--session", help="метка сессии: вечер / ночь")
    s.add_argument("-e", "--estimate", type=float, help="оценка в часах")
    s.add_argument("--column", "-c", default="backlog")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("show", help="карточка + привязанные коммиты")
    s.add_argument("id")
    s.add_argument("--diff", action="store_true", help="показать патч первого коммита")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("move", help="переместить в колонку")
    s.add_argument("id")
    s.add_argument("column", help="id или название колонки")
    s.set_defaults(func=cmd_move)

    s = sub.add_parser("done", help="закрыть карточку")
    s.add_argument("id")
    s.set_defaults(func=cmd_done)

    s = sub.add_parser("edit", help="изменить карточку")
    s.add_argument("id")
    s.add_argument("--title")
    s.add_argument("--desc")
    s.add_argument("--module", "-m", help="пустое значение — снять модуль")
    s.add_argument("--estimate", "-e", type=float)
    s.add_argument("--session")
    s.add_argument("--parent")
    s.set_defaults(func=cmd_edit)

    s = sub.add_parser("rm", help="удалить карточку")
    s.add_argument("id")
    s.add_argument("-y", "--yes", action="store_true", help="без подтверждения")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("attach", help="прикрепить файл")
    s.add_argument("id")
    s.add_argument("path")
    s.add_argument("--name", help="переименовать при копировании")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("detach", help="открепить файл")
    s.add_argument("id")
    s.add_argument("name")
    s.set_defaults(func=cmd_detach)

    s = sub.add_parser("module", help="модули (саб-проекты)")
    msub = s.add_subparsers(dest="module_cmd", required=True)
    ms = msub.add_parser("add")
    ms.add_argument("name")
    ms.add_argument("--color", default="#88ccee")
    ms.add_argument("-d", "--desc")
    ms.set_defaults(func=cmd_module)
    ms = msub.add_parser("list")
    ms.set_defaults(func=cmd_module)
    ms = msub.add_parser("rm")
    ms.add_argument("name")
    ms.set_defaults(func=cmd_module)
    ms = msub.add_parser("rename")
    ms.add_argument("name")
    ms.add_argument("new_name")
    ms.set_defaults(func=cmd_module)
    ms = msub.add_parser("color")
    ms.add_argument("name")
    ms.add_argument("new_name")
    ms.set_defaults(func=cmd_module)
    ms = msub.add_parser("edit")
    ms.add_argument("name")
    ms.add_argument("--name2", dest="new_name")
    ms.add_argument("--color")
    ms.add_argument("-d", "--desc")
    ms.set_defaults(func=cmd_module)

    s = sub.add_parser("serve", help="поднять веб-доску")
    s.add_argument("--host", help="адрес прослушивания (по умолчанию из конфига)")
    s.add_argument("--port", type=int, help="порт (по умолчанию из конфига)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("json", help="выгрузить доску в JSON")
    s.set_defaults(func=cmd_json)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BoardError as exc:
        return die(str(exc))
    except cfgmod.ConfigError as exc:
        return die(str(exc))
    except gitlink.GitError as exc:
        return die(f"git: {exc}")
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return die("прервано")