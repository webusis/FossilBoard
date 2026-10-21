"""Локальный веб-сервер доски: http.server + REST поверх JSON-файла.

Никаких фреймворков, никаких зависимостей, ничего не качается в рантайме.
Слушает 0.0.0.0, чтобы доска открывалась с телефона в той же сети.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from . import config as cfgmod
from . import gitlink
from . import repos as repolib
from .storage import (
    BoardError, add_attempt, add_module, add_task, apply_gotchas, attachment_dir,
    attach_file, delete_attachment, delete_module, delete_task, find_module, find_task,
    get_task, guess_content_type, load_board, move_task, resolve_column, save_board,
    subtasks, task_sort_key, update_module, update_task, wip_state,
)

WEB_DIR = Path(__file__).resolve().parent / "web"

_write_lock = threading.Lock()


class ServeError(Exception):
    """Не удалось поднять сервер: порт занят, нет прав, плохой адрес."""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def upload_limit(cfg: dict | None) -> int:
    """Лимит загрузки вложения: конфиг машины → 64 МБ по умолчанию."""
    mb = int(cfgmod.get(cfg or {}, "attachments.max_upload_mb", 64))
    return mb * 1024 * 1024


def _project_repo(project: str | None, default_repo: str | None, cfg: dict | None = None) -> str:
    """Репозиторий проекта: явный project → текущий проект сервера."""
    if not project:
        if default_repo:
            return default_repo
        raise ApiError(400, "не указан проект")
    proj = cfgmod.project(cfg or {}, project)
    if proj is None:
        raise ApiError(404, f"проект {project} не найден в конфиге")
    return proj["repo_path"]


def board_payload(repo: str) -> dict:
    data = load_board(repo)
    tasks = sorted(data["tasks"], key=task_sort_key)
    head = gitlink.head_commit(repo)
    return {
        "schema_version": data.get("schema_version"),
        "project": data["project"],
        "columns": data["columns"],
        "modules": data["modules"],
        "tasks": tasks,
        "wip": wip_state(data),
        "git_head": head,
        "version": __version__,
    }


def task_payload(repo: str, task_id: str) -> dict:
    data = load_board(repo)
    task = find_task(data, task_id)
    if task is None:
        raise ApiError(404, f"задача {task_id} не найдена")
    kids = subtasks(data, task["id"])
    done_kids = sum(1 for k in kids if k["column_id"] == "done")
    try:
        commits = gitlink.commits_for_task(repo, task["id"])
    except gitlink.GitError as exc:
        commits = []
        commit_error = str(exc)
    else:
        commit_error = None
    return {
        "task": task,
        "module": find_module(data, task.get("module_id") or ""),
        "subtasks": kids,
        "progress": {"done": done_kids, "total": len(kids)},
        "commits": commits,
        "commit_error": commit_error,
        "suggested_commit": f'git commit -m "{task["id"]}: описание"',
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"FossilBoard/{__version__}"
    protocol_version = "HTTP/1.1"
    default_repo: str | None = None
    cfg: dict | None = None      # конфиг машины: проекты, лимиты, порт
    cfg_mtime: float | None = None
    quiet: bool = False  # тесты выключают, чтобы не мусорить в выводе

    def config(self) -> dict:
        """Конфиг машины на каждый раз: проект могли добавить из CLI или из
        другой сессии пока доска открыта, и держать копию в памяти означало
        бы «проекта не видно до перезапуска» (FB-23)."""
        path = cfgmod.config_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if self.cfg is None or mtime != self.cfg_mtime:
            self.cfg = cfgmod.load(path)
            self.cfg_mtime = mtime
        return self.cfg

    # ---- инфраструктура -------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # тише обычного stderr
        if self.quiet:
            return
        if self.path.startswith("/api/"):
            print(f"  {self.command} {self.path} → {args[1] if len(args) > 1 else ''}")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, f"тело запроса не JSON: {exc}") from exc

    def _read_body(self) -> bytes:
        limit = upload_limit(self.config())
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ApiError(413, f"файл больше лимита {limit // (1024 * 1024)} МБ")
        return self.rfile.read(length) if length else b""

    # ---- маршрутизация --------------------------------------------------
    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_PATCH(self) -> None:
        self._route("PATCH")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if not path.startswith("/api/"):
                return self._serve_static(path)
            if method == "GET":
                return self._api_get(path, query)
            if method == "POST":
                return self._api_post(path, query)
            if method in ("PUT", "PATCH"):
                return self._api_update(path, query)
            if method == "DELETE":
                return self._api_delete(path, query)
            raise ApiError(405, f"{method} не поддерживается")
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except repolib.RepoError as exc:
            self._error(400, str(exc))
        except BoardError as exc:
            self._error(400, str(exc))
        except gitlink.GitError as exc:
            self._error(500, f"git: {exc}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # не роняем сервер на странном запросе
            self._error(500, f"{type(exc).__name__}: {exc}")

    # ---- статика --------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            raise ApiError(403, "вне каталога web")
        if not target.is_file():
            raise ApiError(404, f"нет такого файла: {rel}")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or target.suffix in (".js", ".json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ---- GET ------------------------------------------------------------
    def _api_get(self, path: str, q: dict) -> None:
        if path == "/api/state":
            self._json({
                "projects": (self.config()).get("projects", []),
                "current": self.default_repo,
                "config_path": (self.config()).get("_path"),
                "version": __version__,
            })
            return
        if path == "/api/board":
            self._json(board_payload(_project_repo(q.get("project"), self.default_repo, self.config())))
            return
        if path == "/api/task":
            repo = _project_repo(q.get("project"), self.default_repo, self.config())
            self._json(task_payload(repo, q.get("id", "")))
            return
        if path == "/api/commits":
            repo = _project_repo(q.get("project"), self.default_repo, self.config())
            commits = gitlink.commits_for_task(repo, q.get("id", ""))
            self._json({"commits": commits})
            return
        if path == "/api/diff":
            repo = _project_repo(q.get("project"), self.default_repo, self.config())
            rev = q.get("rev", "")
            if not rev:
                raise ApiError(400, "нужен rev")
            self._json(gitlink.show_commit(repo, rev))
            return
        if path == "/api/browse":
            self._json(repolib.browse(q.get("path")))
            return
        if path == "/api/repo":
            raw = q.get("path", "")
            self._json(repolib.describe(repolib.expand(raw)))
            return
        if path.startswith("/api/attachments/"):
            # /api/attachments/<task-id>/<имя файла>
            tail = path[len("/api/attachments/"):]
            task_id, _, name = tail.partition("/")
            self._send_attachment(_project_repo(q.get("project"), self.default_repo, self.config()),
                                  task_id, name)
            return
        raise ApiError(404, f"нет такого маршрута: {path}")

    def _send_attachment(self, repo: str, task_id: str, name: str) -> None:
        from .storage import safe_name
        fname = safe_name(name)
        target = (attachment_dir(repo, task_id) / fname).resolve()
        root = attachment_dir(repo, task_id).resolve()
        if root not in target.parents:
            raise ApiError(403, "путь за пределами каталога вложений")
        if not target.is_file():
            raise ApiError(404, f"вложение не найдено: {task_id}/{fname}")
        data = target.read_bytes()
        ctype = guess_content_type(fname)
        disp = "inline" if ctype.startswith("image/") else "attachment"
        self._send(200, data, ctype,
                   {"Content-Disposition": f'{disp}; filename="{fname}"'})

    # ---- POST -----------------------------------------------------------
    def _api_post(self, path: str, q: dict) -> None:
        if path == "/api/projects":
            return self._api_project(body=self._read_json())
        repo = _project_repo(q.get("project"), self.default_repo, self.config())
        with _write_lock:
            data = load_board(repo)
            if path == "/api/tasks":
                body = self._read_json()
                task = add_task(
                    data, body.get("title", ""),
                    description=body.get("description", ""),
                    module_id=body.get("module_id"),
                    parent_id=body.get("parent_id"),
                    estimate=body.get("estimate"),
                    session_label=body.get("session_label"),
                    column_id=body.get("column_id") or "backlog",
                )
                save_board(repo, data)
                self._json(task_payload(repo, task["id"]), 201)
                return
            if path.startswith("/api/tasks/") and path.endswith("/sync"):
                # разбор истории: грабли из флагов в сообщениях коммитов
                task_id = path[len("/api/tasks/"):-len("/sync")]
                card = get_task(data, task_id)
                registry = cfgmod.flags_registry(self.config())
                items = gitlink.collect_gotchas(repo, data, registry, only=card["id"])
                written = apply_gotchas(data, items)
                save_board(repo, data)
                self._json({"written": written, **task_payload(repo, card["id"])}, 201)
                return
            if path.startswith("/api/tasks/") and path.endswith("/fail"):
                # запись неудачной попытки: колонку карточки не трогаем
                task_id = path[len("/api/tasks/"):-len("/fail")]
                body = self._read_json()
                attempt = add_attempt(
                    get_task(data, task_id), body.get("text", ""),
                    why=body.get("why", ""), tried=body.get("tried", ""),
                    tags=body.get("tags") or [],
                )
                save_board(repo, data)
                self._json({"attempt": attempt, **task_payload(repo, task_id)}, 201)
                return
            if path.startswith("/api/tasks/") and path.endswith("/move"):
                task_id = path[len("/api/tasks/"):-len("/move")]
                body = self._read_json()
                move_task(data, task_id, body.get("column_id", ""))
                save_board(repo, data)
                self._json(task_payload(repo, task_id))
                return
            if path.startswith("/api/tasks/") and path.endswith("/attachments"):
                task_id = path[len("/api/tasks/"):-len("/attachments")]
                name = q.get("name") or (self.headers.get("X-File-Name") or "")
                body = self._read_body()
                if not name or not body:
                    raise ApiError(400, "нужно имя файла и тело запроса")
                tmp = Path("/tmp") / f"fossilboard-upload-{threading.get_ident()}-{len(body)}"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(body)
                try:
                    item = attach_file(repo, task_id, tmp, name=name)
                finally:
                    tmp.unlink(missing_ok=True)
                self._json({"attachment": item, **task_payload(repo, task_id)}, 201)
                return
            if path == "/api/modules":
                body = self._read_json()
                mod = add_module(data, body.get("name", ""), color=body.get("color", "#88ccee"),
                                 description=body.get("description", ""))
                save_board(repo, data)
                self._json(mod, 201)
                return
        raise ApiError(404, f"нет такого маршрута: {path}")

    def _api_project(self, body: dict) -> None:
        """Создать новую репу с доской или подключить существующую.

        Конфиг здесь единственное место, куда пишется «что у меня есть»,
        поэтому после успеха обновляем и его в памяти — иначе /api/state
        продолжит показывать старый список.
        """
        raw = body.get("path", "")
        with _write_lock:
            cfg = self.config()
            result = repolib.attach(cfg, raw,
                                    project_id=(body.get("project_id") or "").strip() or None,
                                    name=(body.get("name") or "").strip() or None,
                                    create=bool(body.get("create")))
            self.cfg = cfg
        self._json(result, 201)

    # ---- PUT/PATCH ------------------------------------------------------
    def _api_update(self, path: str, q: dict) -> None:
        repo = _project_repo(q.get("project"), self.default_repo, self.config())
        body = self._read_json()
        with _write_lock:
            data = load_board(repo)
            if path.startswith("/api/tasks/"):
                task_id = path[len("/api/tasks/"):]
                changes = {k: body.get(k) for k in
                           ("title", "description", "column_id", "module_id", "parent_id",
                            "estimate", "session_label") if k in body}
                update_task(data, task_id, changes)
                save_board(repo, data)
                self._json(task_payload(repo, task_id))
                return
            if path.startswith("/api/modules/"):
                mod_ref = path[len("/api/modules/"):]
                mod = update_module(data, mod_ref, body)
                save_board(repo, data)
                self._json(mod)
                return
        raise ApiError(404, f"нет такого маршрута: {path}")

    # ---- DELETE ---------------------------------------------------------
    def _api_delete(self, path: str, q: dict) -> None:
        repo = _project_repo(q.get("project"), self.default_repo, self.config())
        with _write_lock:
            data = load_board(repo)
            if path.startswith("/api/tasks/"):
                task_id = path[len("/api/tasks/"):]
                removed = delete_task(data, task_id)
                save_board(repo, data)
                self._json({"removed": removed})
                return
            if path == "/api/attachments":
                delete_attachment(repo, q.get("id", ""), q.get("name", ""))
                self._json({"ok": True})
                return
            if path.startswith("/api/modules/"):
                mod = delete_module(data, path[len("/api/modules/"):])
                save_board(repo, data)
                self._json({"removed": mod["id"]})
                return
        raise ApiError(404, f"нет такого маршрута: {path}")


def serve(repo: str | None = None, host: str | None = None, port: int | None = None,
          cfg: dict | None = None) -> int:
    """Поднять доску. Без явных host/port берёмся из конфига машины."""
    cfg = cfg if cfg is not None else cfgmod.load()
    host, port = cfgmod.effective_server(cfg, host, port)
    Handler.default_repo = str(Path(repo).resolve()) if repo else None
    Handler.cfg = cfg
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise ServeError(
            f"порт занят или недоступен: {host}:{port} — {exc.strerror or exc}"
        ) from exc
    shown = "localhost" if host in ("0.0.0.0", "") else host
    print(f"FossilBoard {__version__} — http://{shown}:{port}")
    print(f"конфиг: {cfg.get('_path', '—')}")
    if Handler.default_repo:
        print(f"проект: {Handler.default_repo}")
    else:
        print(f"проектов в конфиге: {len(cfg.get('projects', []))} — выбор в шапке доски")
    print("Ctrl+C — остановить")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        httpd.server_close()
    return 0