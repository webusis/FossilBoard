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
from . import hooks
from .storage import (
    BoardError, add_attempt, add_event, add_gotcha_once, add_module, add_task,
    apply_gotchas, attach_file, board_dir, board_path, column_name, delete_attachment,
    delete_module, delete_task, find_module, find_task, attempts_of, get_task, gotchas_of,
    init_board, load_board, move_task, normalize_task_id, resolve_column, save_board,
    subtasks, task_prefix, update_module, update_task, wip_state,
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
        ids = [p["id"] for p in projects if isinstance(p, dict)]
        hint = f"добавь --project {ids[0]}"
        if not cfg.get("default_project") and len(ids) > 1:
            hint += (f" (или один раз: board.py config set default_project {ids[0]})")
        raise BoardError(f"проект не указан, в конфиге их несколько ({', '.join(ids)}) — {hint}")
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
    gotchas = gotchas_of(task)
    if gotchas:
        lines.append(pad + f"  грабли ({len(gotchas)}):")
        for g in gotchas:
            flag = f"#{g['flag']} " if g.get("flag") else ""
            src = f"  [{g.get('source', 'manual')}]" if g.get("source") else ""
            lines.append(pad + f"    {flag}{g['text']}{src}")
    attempts = attempts_of(task)
    if attempts:
        lines.append(pad + f"  неудачные пути ({len(attempts)}):")
        for a in attempts:
            why = f" — {a['why']}" if a.get("why") else ""
            tags = ("  " + ", ".join(f"#{t}" for t in a.get("tags", []))) if a.get("tags") else ""
            lines.append(pad + f"    {a['text']}{why}{tags}")
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
    repo_now = str(Path(args.repo).expanduser().resolve())
    if existed and data["project"].get("repo_path") != repo_now:
        # путь хранится и в самой доске; обновляем ДО регистрации проекта,
        # иначе в конфиг попадёт старый путь (пойман на живом клоне)
        data["project"]["repo_path"] = repo_now
        save_board(args.repo, data)
    cfg = get_config()
    proj = cfgmod.upsert_project(cfg, data["project"]["id"], data["project"]["name"], repo_now)
    cfgmod.save(cfg)
    if existed:
        if args.id and args.id.upper() != data["project"]["id"]:
            print(f"внимание: доска уже настроена на префикс {data['project']['id']}, "
                  f"а в команде указан {args.id.upper()} — оставляю {data['project']['id']}",
                  file=sys.stderr)
        print(f"доска уже была: {path}")
        print(f"данные не тронуты, репозиторий перерегистрирован: {repo_now}")
    else:
        print(f"доска создана: {path}")
        print(f"проект: {proj['id']} «{proj['name']}» → {proj['repo_path']}")
    _install_hook_quiet(repo_now)
    return EXIT_OK


def _install_hook_quiet(repo_path: str) -> None:
    """Хук post-commit: грабли из коммитов подхватываются сами.

    Ставится молча, но с подтверждением в выводе — про него не надо помнить.
    """
    try:
        path = hooks.install(repo_path, hook_log_path())
        print(f"хук post-commit установлен: {path} (грабли будут подхватываться сами)")
    except OSError as exc:
        print(f"хук не поставился: {exc}", file=sys.stderr)


def cmd_projects(args: argparse.Namespace) -> int:
    cfg = get_config()
    action = getattr(args, "projects_cmd", "list")
    if action == "add":
        if not (args.id and args.repo):
            return die("нужно: board.py projects add FB \"Имя\" --repo /путь/к/репе")
        repo_path = Path(args.repo).expanduser().resolve()
        board = board_path(repo_path)
        if not board.is_file():
            print(f"внимание: в {repo_path} нет доски — доска будет создана")
        proj = cfgmod.upsert_project(cfg, args.id, args.name or args.id, str(repo_path))
        if not board.is_file():
            data = init_board(repo_path, proj["id"], proj["name"], task_prefix=proj["id"])
            # путь в доске должен совпадать с тем, что в конфиге
            data["project"]["repo_path"] = str(repo_path)
            save_board(repo_path, data)
        cfgmod.save(cfg)
        print(f"{proj['id']}  {proj['name']}  →  {proj['repo_path']}")
        print(f"конфиг: {cfg['_path']}")
        _install_hook_quiet(str(repo_path))
        return EXIT_OK
    if action == "rm":
        if not args.id:
            return die("нужен id: board.py projects rm FB")
        if not cfgmod.remove_project(cfg, args.id):
            return die(f"проект {args.id} в конфиге не найден")
        cfgmod.save(cfg)
        print(f"проект {args.id} убран из конфига (доска в репе не тронута)")
        return EXIT_OK
    if action == "show":
        proj = cfgmod.project(cfg, args.id)
        if proj is None:
            return die(f"проект {args.id} не найден")
        print(json.dumps(proj, indent=2, sort_keys=True, ensure_ascii=False))
        return EXIT_OK

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
    """Показать, создать или поправить конфиг. По умолчанию — в проекте."""
    cfg = get_config()
    if args.config_cmd == "path":
        print(cfg["_path"])
        return EXIT_OK
    if args.config_cmd == "where":
        print("конфиг ищется в таком порядке:")
        for cand in cfgmod.candidate_paths():
            mark = "← используется" if str(cand) == cfg["_path"] else (
                "" if cand.is_file() else "(нет файла)")
            print(f"  {cand}  {mark}")
        print(f"шаблон: {cfgmod.template_path()}")
        return EXIT_OK
    if args.config_cmd == "init":
        target = Path(args.path).expanduser() if args.path else cfgmod.local_config_path()
        if target.exists():
            return die(f"уже есть: {target}")
        cfgmod.save(cfg, target)
        print(f"конфиг создан: {target}")
        print(f"шаблон с пояснениями: {cfgmod.template_path()}")
        print("этот файл можно коммитить вместе с проектом — настройки поедут с ним")
        return EXIT_OK
    if args.config_cmd == "template":
        target = Path(args.path).expanduser() if args.path else Path.cwd() / cfgmod.TEMPLATE_NAME
        src = cfgmod.template_path()
        if not src.is_file():
            return die(f"шаблон не найден в репозитории: {src}")
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"шаблон скопирован: {src} → {target}")
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
        # застрявшая задача должна быть видна сразу: сколько граблей и
        # сколько неудачных путей
        gotchas = len(gotchas_of(task))
        fails = len(attempts_of(task))
        marks = (f"  ⚠{gotchas}" if gotchas else "") + (f"  ✗{fails}" if fails else "")
        print(f"{mark} {task['id']:<10} {column_name(data, task['column_id']):<12} "
              f"{task['title']}{est}{ses}{marks}")
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
                    session_label=args.session, column_id=args.column or "backlog",
                    explicit_id=args.id)
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


def cmd_hook(args: argparse.Namespace) -> int:
    """Хук git: после коммита грабли подхватываются сами."""
    repo, _ = load(args)
    action = getattr(args, "hook_cmd", "status")
    if action == "install":
        log = hook_log_path()
        try:
            path = hooks.install(repo, log)
        except hooks.HookError as exc:
            return die(str(exc))
        print(f"хук установлен: {path}")
        print(f"лог: {log}")
        print("теперь грабли из коммитов попадают в карточку сами")
        return EXIT_OK
    if action == "remove":
        if hooks.remove(repo):
            print("хук снят")
        else:
            print("хук не установлен (или он не наш — не трогаю)")
        return EXIT_OK
    print(f"хук {'установлен' if hooks.is_installed(repo) else 'не установлен'}: "
          f"{hooks.hook_path(repo)}")
    return EXIT_OK


def hook_log_path() -> Path:
    """Лог хука — рядом с логами доски, из конфига машины."""
    base = cfgmod.get(get_config(), "paths.logs", "~/.local/state/fossilboard/logs")
    path = Path(str(base)).expanduser() / "hook.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def cmd_sync(args: argparse.Namespace) -> int:
    """Разобрать историю репы и дописать грабли из флагов в карточки.

        git commit -m "FB-12 #rake Тк не thread-safe"

    Флаг без привязки к карточке не принимается. Повторный запуск ничего
    не портит: грабля с тем же флагом и тем же текстом уже есть — не пишем.
    """
    repo, data = load(args)
    registry = cfgmod.flags_registry(get_config())
    only = None
    if args.id:
        only = normalize_task_id(data, args.id)
        get_task(data, only)          # понятная ошибка, если карточки нет
    if only:
        # только коммиты этой карточки: вся история репы не нужна
        items = [{"card_id": only, **f}
                 for f in gitlink.flags_for_task(repo, only, registry)]
    else:
        items = gitlink.collect_gotchas(repo, data, registry)
    written = apply_gotchas(data, items)
    for item in written:
        print(f"{item['card_id']} ← #{item['flag']} {item['text']}  ({item['commit']})")
    if not args.dry_run:
        save_board(repo, data)
    print(f"граблей добавлено: {len(written)}")
    return EXIT_OK


def cmd_fail(args: argparse.Namespace) -> int:
    """Записать неудачную попытку в карточку.

    Это запись, а не «закрыть с ошибкой»: колонка, оценка и модуль не
    трогаются. Знать, куда НЕ походили, часто ценнее знать финальный маршрут.
    """
    repo, data = load(args)
    task = find_task(data, args.id)
    if task is None:
        return die(f"задача {args.id} не найдена")
    text = (args.text or "").strip()
    if not text:
        return die("пусто — нечего записывать")
    attempt = add_attempt(task, text, why=args.why or "", tried=args.tried or "",
                          tags=args.tag or [])
    add_event(task, "failed", text)
    save_board(repo, data)
    print(f"{task['id']} ✗ {attempt['text']}")
    if attempt["why"]:
        print(f"   почему: {attempt['why']}")
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
    # серверу конкретный проект не нужен: он показывает список и работает с
    # любым через ?project=. Если проектов несколько, это не ошибка, а
    # выбор пользователя в шапке доски
    try:
        repo = resolve_repo(args)
    except BoardError:
        repo = None
        if args.project:
            print(f"проект {args.project} не найден в конфиге", file=sys.stderr)
        else:
            print("в конфиге несколько проектов — доска покажет их все, "
                  "переключение в шапке")
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
    psub = s.add_subparsers(dest="projects_cmd")
    ps = psub.add_parser("add", help="добавить проект (и доску, если её нет)")
    ps.add_argument("id", help="префикс карточек, напр. FB")
    ps.add_argument("name", nargs="?", help="название проекта")
    ps.add_argument("--repo", required=True, help="путь к репозиторию")
    ps.set_defaults(func=cmd_projects, projects_cmd="add")
    ps = psub.add_parser("rm", help="убрать проект из конфига")
    ps.add_argument("id")
    ps.set_defaults(func=cmd_projects, projects_cmd="rm")
    ps = psub.add_parser("show", help="показать проект в виде JSON")
    ps.add_argument("id")
    ps.set_defaults(func=cmd_projects, projects_cmd="show")
    s.set_defaults(func=cmd_projects, projects_cmd="list")

    s = sub.add_parser("config", help="конфиг: пути, проекты, порт")
    csub = s.add_subparsers(dest="config_cmd")
    cs = csub.add_parser("path", help="какой конфиг сейчас используется")
    cs.set_defaults(func=cmd_config)
    cs = csub.add_parser("where", help="где доска ищет конфиг и в каком порядке")
    cs.set_defaults(func=cmd_config)
    cs = csub.add_parser("init", help="создать config.json в текущем каталоге")
    cs.add_argument("--path", help="куда создать (по умолчанию ./config.json)")
    cs.set_defaults(func=cmd_config)
    cs = csub.add_parser("template", help="скопировать config.example.json рядом с собой")
    cs.add_argument("--path", help="куда скопировать (по умолчанию ./config.example.json)")
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
    s.add_argument("--id", dest="id", help="конкретный номер карточки (FB-12 или 12)")
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

    s = sub.add_parser("hook", help="хук git: грабли из коммитов подхватываются сами")
    hsub = s.add_subparsers(dest="hook_cmd")
    hsub.add_parser("install", help="поставить post-commit")
    hsub.add_parser("remove", help="снять post-commit")
    hsub.add_parser("status", help="есть ли хук")
    s.set_defaults(func=cmd_hook, hook_cmd="status")

    s = sub.add_parser("sync", help="разобрать историю и дописать грабли из флагов")
    s.add_argument("id", nargs="?", help="только эта карточка (по умолчанию — все)")
    s.add_argument("--dry-run", action="store_true", help="показать и не писать")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("fail", help="записать неудачную попытку в карточку")
    s.add_argument("id")
    s.add_argument("text", help="что пробовали")
    s.add_argument("--why", help="почему не получилось")
    s.add_argument("--tried", help="чем именно пробовали")
    s.add_argument("--tag", action="append", help="метка (можно повторять)")
    s.set_defaults(func=cmd_fail)

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