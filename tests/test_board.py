"""Тесты FossilBoard. Только stdlib unittest, без зависимостей.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from pathlib import Path

from fossilboard import config as config_mod
from fossilboard import gitlink
from fossilboard import repos as repolib
from fossilboard import storage
from fossilboard.storage import (
    BoardError, add_module, add_task, atomic_write_json, delete_attachment, delete_task,
    find_task, init_board, load_board, move_task, resolve_column, save_board, subtasks,
    task_num, update_task, wip_state,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="Тест", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="Тест", GIT_COMMITTER_EMAIL="t@example.com")
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=env, check=True)
    return proc.stdout


class TempRepoCase(unittest.TestCase):
    """Каждый тест — свежая временная репа с инициализированной доской."""

    prefix = "K2C"
    board_name = "Тестовый проект"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fossilboard-test-"))
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Тест")
        self.data = init_board(self.repo, self.prefix, self.board_name)
        # каждый тест обязан работать со своим конфигом: иначе поднимет
        # projects/attach из теста и уедет в боевой ~/.config/fossilboard,
        # после чего доска Артёма показывает мусорные проекты из /tmp
        self._env_guard = mock.patch.dict(
            os.environ, {"FOSSILBOARD_CONFIG": str(self.tmp / "config.json")})
        self._env_guard.start()

    def tearDown(self):
        self._env_guard.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self):
        save_board(self.repo, self.data)

    def cli_env(self) -> dict:
        """Окружение для subprocess: конфиг — временный.

        Без этого тесты писали бы проекты в настоящий ~/.config/fossilboard.
        """
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        env.setdefault("FOSSILBOARD_CONFIG", str(self.tmp / "config.json"))
        return env

    def new_task(self, title="Задача", **kw):
        return add_task(self.data, title, **kw)


class TestCrud(TempRepoCase):
    def test_add_creates_task_with_correct_id(self):
        task = self.new_task("Первая")
        self.assertEqual(task["id"], "K2C-1")
        self.assertEqual(task["column_id"], "backlog")
        self.assertEqual(task["events"][0]["kind"], "created")
        self.assertTrue(storage.board_path(self.repo).exists())
        self.data and self.save()
        reloaded = load_board(self.repo)
        self.assertEqual(len(reloaded["tasks"]), 1)
        self.assertEqual(reloaded["tasks"][0]["title"], "Первая")

    def test_ids_are_not_reused_after_delete(self):
        first = self.new_task("Первая")
        second = self.new_task("Вторая")
        self.assertEqual([first["id"], second["id"]], ["K2C-1", "K2C-2"])
        delete_task(self.data, first["id"])
        third = self.new_task("Третья")
        self.assertEqual(third["id"], "K2C-3", "номер удалённой карточки не должен возвращаться")
        self.save()
        self.assertEqual(load_board(self.repo)["counters"]["task"], 3)

    def test_edit_task(self):
        task = self.new_task("Черновик")
        update_task(self.data, task["id"], {"title": "Готово", "estimate": 2.5,
                                            "session_label": "вечер"})
        self.save()
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(fresh["title"], "Готово")
        self.assertEqual(fresh["estimate"], 2.5)
        self.assertEqual(fresh["session_label"], "вечер")
        self.assertIn("edited", [e["kind"] for e in fresh["events"]])

    def test_edit_rejects_unknown_module(self):
        task = self.new_task("X")
        with self.assertRaises(BoardError):
            update_task(self.data, task["id"], {"module_id": "нет-такого"})

    def test_delete_removes_subtasks_too(self):
        parent = self.new_task("Родитель")
        child = self.new_task("Саб", parent_id=parent["id"])
        removed = delete_task(self.data, parent["id"])
        self.assertEqual(sorted(removed), sorted([parent["id"], child["id"]]))
        self.assertEqual(self.data["tasks"], [])

    def test_unknown_task_raises(self):
        with self.assertRaises(BoardError):
            self.new_task("x")
            delete_task(self.data, "K2C-999")

    def test_column_can_be_named_or_id(self):
        self.assertEqual(resolve_column(self.data, "wip"), "wip")
        self.assertEqual(resolve_column(self.data, "В работе"), "wip")
        with self.assertRaises(BoardError):
            resolve_column(self.data, "нет")


class TestMoveAndProgress(TempRepoCase):
    def test_move_between_columns_and_events(self):
        task = self.new_task("Двигать")
        move_task(self.data, task["id"], "wip")
        move_task(self.data, task["id"], "done")
        self.save()
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(fresh["column_id"], "done")
        self.assertIsNotNone(fresh["closed_at"], "закрытая карточка обязана иметь closed_at")
        kinds = [e["kind"] for e in fresh["events"]]
        self.assertIn("moved", kinds)
        self.assertIn("closed", kinds)

    def test_move_out_of_done_clears_closed_at(self):
        task = self.new_task("Туда-обратно")
        move_task(self.data, task["id"], "done")
        move_task(self.data, task["id"], "review")
        self.assertIsNone(self.data["tasks"][0]["closed_at"])

    def test_subtask_progress(self):
        parent = self.new_task("Родитель")
        kids = [self.new_task(f"Саб {i}", parent_id=parent["id"]) for i in range(5)]
        for kid in kids[:3]:
            move_task(self.data, kid["id"], "done")
        found = subtasks(self.data, parent["id"])
        done = sum(1 for k in found if k["column_id"] == "done")
        self.assertEqual((done, len(found)), (3, 5))

    def test_wip_limit_flag(self):
        for i in range(3):
            self.new_task(f"Работа {i}", column_id="wip")
        wip = wip_state(self.data)
        self.assertEqual(wip["wip"]["count"], 3)
        self.assertTrue(wip["wip"]["over"], "3 карточки при лимите 2 — превышение")
        self.assertFalse(wip["backlog"]["over"])


class TestModules(TempRepoCase):
    def test_module_crud(self):
        mod = add_module(self.data, "Ядро", color="#88ccee")
        task = self.new_task("Карточка", module_id=mod["id"])
        self.assertEqual(task["module_id"], mod["id"])
        storage.update_module(self.data, "Ядро", {"color": "#ff0000", "name": "Движок"})
        self.assertEqual(storage.find_module(self.data, "Движок")["color"], "#ff0000")
        storage.delete_module(self.data, mod["id"])
        self.assertEqual(self.data["modules"], [])
        self.assertIsNone(self.data["tasks"][0]["module_id"], "карточка остаётся, модуль снимается")

    def test_duplicate_module_rejected(self):
        add_module(self.data, "UI")
        with self.assertRaises(BoardError):
            add_module(self.data, "UI")


class TestAtomicWrite(TempRepoCase):
    def test_file_is_valid_json_and_sorted_after_write(self):
        self.new_task("Задача")
        self.save()
        raw = storage.board_path(self.repo).read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(list(data), sorted(data), "ключи должны быть отсортированы для читаемого diff")
        self.assertIn('\n  "schema_version"', raw, "JSON должен быть отформатирован отступами")

    def test_no_temp_files_left_behind(self):
        self.save()
        leftovers = [p.name for p in storage.board_dir(self.repo).iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_write_is_replace_not_truncate(self):
        path = storage.board_path(self.repo)
        first = path.read_bytes()
        add_task(self.data, "Ещё одна")
        self.save()
        self.assertNotEqual(path.read_bytes(), first)
        self.assertEqual(len(list(storage.board_dir(self.repo).glob("*.tmp"))), 0)

    def test_atomic_write_replaces_broken_file_wholesale(self):
        path = storage.board_dir(self.repo) / "other.json"
        path.write_text("{ сломан", encoding="utf-8")
        atomic_write_json(path, {"a": 1, "b": [1, 2]})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1, "b": [1, 2]})


class TestBrokenJsonRecovery(TempRepoCase):
    def test_broken_json_raises_with_recovery_hint(self):
        self.save()
        storage.board_path(self.repo).write_text('{"tasks": [', encoding="utf-8")
        with self.assertRaises(BoardError) as ctx:
            load_board(self.repo)
        message = str(ctx.exception)
        self.assertIn("битый JSON", message)
        self.assertIn("git -C", message, "в ошибке должна быть подсказка восстановления из git")
        self.assertIn("show HEAD:.board/board.json", message)

    def test_missing_board_suggests_init(self):
        path = storage.board_path(self.repo)
        path.unlink()
        with self.assertRaises(BoardError) as ctx:
            load_board(self.repo)
        self.assertIn("init", str(ctx.exception))

    def test_empty_file_suggests_recovery(self):
        storage.board_path(self.repo).write_text("", encoding="utf-8")
        with self.assertRaises(BoardError) as ctx:
            load_board(self.repo)
        self.assertIn("git -C", str(ctx.exception))

    def test_recovery_from_git_actually_restores_board(self):
        self.new_task("Живая задача")
        self.save()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "борд")
        storage.board_path(self.repo).write_text("{ сломан", encoding="utf-8")
        with self.assertRaises(BoardError):
            load_board(self.repo)
        restored = subprocess.run(
            f"git -C {self.repo} show HEAD:.board/board.json",
            shell=True, capture_output=True, text=True, check=True).stdout
        storage.board_path(self.repo).write_text(restored, encoding="utf-8")
        board = load_board(self.repo)
        self.assertEqual(board["tasks"][0]["title"], "Живая задача")


class TestAttachments(TempRepoCase):
    def test_attach_copies_into_task_folder(self):
        task = self.new_task("Со скрином")
        self.save()
        src = Path(self.tmp) / "shot.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 500)
        item = storage.attach_file(self.repo, task["id"], src)
        target = storage.board_dir(self.repo) / "attachments" / task["id"] / "shot.png"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), src.read_bytes())
        self.assertEqual(item["path"], f"attachments/{task['id']}/shot.png")
        self.assertEqual(item["size"], 508)
        board = load_board(self.repo)
        self.assertEqual(board["tasks"][0]["attachments"][0]["name"], "shot.png")
        self.assertIn("attach", [e["kind"] for e in board["tasks"][0]["events"]])

    def test_attach_renames_and_sanitises(self):
        task = self.new_task("X")
        self.save()
        src = Path(self.tmp) / "screenshot 2026 10 17.png"
        src.write_bytes(b"png")
        item = storage.attach_file(self.repo, task["id"], src, name="../../FB-1-after.png")
        self.assertEqual(item["name"], "FB-1-after.png")
        on_disk = list((storage.board_dir(self.repo) / "attachments" / task["id"]).iterdir())
        self.assertEqual([p.name for p in on_disk], ["FB-1-after.png"])

    def test_attach_missing_file_raises(self):
        task = self.new_task("X")
        with self.assertRaises(BoardError):
            storage.attach_file(self.repo, task["id"], self.tmp / "нет-такого.png")

    def test_delete_attachment_removes_file_and_entry(self):
        task = self.new_task("X")
        self.save()
        src = Path(self.tmp) / "a.txt"
        src.write_text("привет", encoding="utf-8")
        storage.attach_file(self.repo, task["id"], src)
        delete_attachment(self.repo, task["id"], "a.txt")
        self.assertFalse((storage.board_dir(self.repo) / "attachments" / task["id"] / "a.txt").exists())
        self.assertEqual(load_board(self.repo)["tasks"][0]["attachments"], [])

    def test_content_type_for_images(self):
        self.assertEqual(storage.guess_content_type("FB-1-after.png"), "image/png")
        self.assertEqual(storage.guess_content_type("a.txt"), "text/plain")
        self.assertEqual(storage.guess_content_type("a.bin"), "application/octet-stream")


class TestGitLink(TempRepoCase):
    def make_commit(self, message: str, filename: str = "code.py") -> str:
        (self.repo / filename).write_text(f"# {message}\nprint({len(message)})\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD").strip()

    def test_commit_with_hashtag_is_found(self):
        task = self.new_task("С привязкой")
        self.make_commit(f"{task['id']}: добавил модуль разбора")
        commits = gitlink.commits_for_task(self.repo, task["id"])
        self.assertEqual(len(commits), 1)
        self.assertIn(task["id"], commits[0]["subject"])
        self.assertEqual(commits[0]["author"], "Тест")

    def test_commit_without_hashtag_is_not_found(self):
        task = self.new_task("Тихая")
        self.make_commit("просто коммит без привязки")
        self.assertEqual(gitlink.commits_for_task(self.repo, task["id"]), [])

    def test_hashtag_match_is_exact_not_prefix(self):
        first = self.new_task("Первая")
        self.make_commit(f"{first['id']}: коммит")
        # K2C-1 не должен цеплять коммит K2C-12
        (self.repo / "x.py").write_text("x = 1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "K2C-12: другая карточка")
        found = gitlink.commits_for_task(self.repo, first["id"])
        self.assertEqual(len(found), 1)
        self.assertNotIn("K2C-12", found[0]["subject"])

    def test_diff_is_returned_and_truncation_is_honest(self):
        rev = self.make_commit("K2C-1: патч")
        diff = gitlink.show_commit(self.repo, rev)
        self.assertIn("+print(", diff["text"])
        self.assertIn("---", diff["text"])
        self.assertFalse(diff["truncated"])

        (self.repo / "big.py").write_text("".join(f"line {i}\n" for i in range(5000)), encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "K2C-1: большой патч")
        rev2 = git(self.repo, "rev-parse", "HEAD").strip()
        big = gitlink.show_commit(self.repo, rev2, max_lines=200)
        self.assertTrue(big["truncated"])
        self.assertIn("обрезан", big["text"])
        self.assertLessEqual(len(big["text"].splitlines()), 205)

    def test_commit_found_on_other_branch(self):
        task = self.new_task("Ветка")
        self.make_commit(f"{task['id']}: в main")
        git(self.repo, "checkout", "-q", "-b", "feature")
        self.make_commit(f"{task['id']}: в feature")
        commits = gitlink.commits_for_task(self.repo, task["id"])
        self.assertEqual(len(commits), 2, "--all должен видеть все ветки")

    def test_stash_commits_are_not_task_links(self):
        """git stash к задаче отношения не имеет, даже если в тексте есть #ID."""
        task = self.new_task("Без мусора")
        self.make_commit(f"{task['id']}: настоящая работа")
        (self.repo / "code.py").write_text("# черновик\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "stash", "-q", "-m", f"{task['id']}: черновик")
        found = gitlink.commits_for_task(self.repo, task["id"])
        subjects = [c["subject"] for c in found]
        self.assertEqual(len(subjects), 1, f"stash не должен считаться привязкой: {subjects}")
        self.assertIn("настоящая работа", subjects[0])
        self.assertTrue(gitlink.is_stash("WIP on master: feat: что-то (#FB-1):"))
        self.assertTrue(gitlink.is_stash("index on master: FB-1: правка"))
        self.assertFalse(gitlink.is_stash("feat: FB-1: правка"))
        self.assertFalse(gitlink.is_stash("WIP на доске: FB-1"))

    def test_head_commit(self):
        self.assertIsNone(gitlink.head_commit(self.repo))
        self.make_commit("первый")
        self.assertTrue(gitlink.head_commit(self.repo))


class TestServer(TempRepoCase):
    """Поднимаем настоящий сервер на эфемерном порту и бьём по HTTP."""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from fossilboard.server import Handler
        cls.Handler = Handler

    def start(self):
        import threading
        from http.server import ThreadingHTTPServer
        self.Handler.default_repo = str(self.repo)
        self.Handler.quiet = True
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, url):
        with urlopen(url) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()

    def request(self, url, method="GET", payload=None, ctype="application/json"):
        data = None if payload is None else (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        req = Request(url, data=data, method=method, headers={"Content-Type": ctype})
        try:
            with urlopen(req) as resp:
                body = resp.read()
                return resp.status, json.loads(body) if body else None
        except HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_index_html_is_served(self):
        base = self.start()
        try:
            status, ctype, body = self.get(base + "/")
            self.assertEqual(status, 200)
            self.assertIn("text/html", ctype)
            self.assertIn(b"FossilBoard", body)
        finally:
            self.stop()

    def test_board_and_task_api(self):
        base = self.start()
        try:
            status, data = self.request(base + "/api/board")
            self.assertEqual(status, 200)
            self.assertEqual(data["project"]["id"], "K2C")
            status, created = self.request(base + "/api/tasks", "POST", {"title": "Из HTTP"})
            self.assertEqual(status, 201)
            self.assertEqual(created["task"]["title"], "Из HTTP")
            status, moved = self.request(
                base + f"/api/tasks/{created['task']['id']}/move", "POST", {"column_id": "wip"})
            self.assertEqual(moved["task"]["column_id"], "wip")
            status, deleted = self.request(base + f"/api/tasks/{created['task']['id']}", "DELETE")
            self.assertEqual(status, 200)
            self.assertEqual(deleted["removed"], [created["task"]["id"]])
        finally:
            self.stop()

    def test_image_attachment_served_as_binary(self):
        base = self.start()
        try:
            self.new_task("С картинкой")
            self.save()
            png = (b"\x89PNG\r\n\x1a\n" + b"payload" * 100)
            url = base + f"/api/tasks/K2C-1/attachments?name=FB-1-after.png"
            status, data = self.request(url, "POST", png, "application/octet-stream")
            self.assertEqual(status, 201)
            self.assertEqual(data["attachment"]["name"], "FB-1-after.png")
            self.assertEqual(data["attachment"]["size"], len(png))

            status, ctype, body = self.get(base + "/api/attachments/K2C-1/FB-1-after.png")
            self.assertEqual(status, 200)
            self.assertEqual(ctype, "image/png")
            self.assertEqual(body, png, "картинка должна отдаваться байт-в-байт, а не как JSON")
            self.assertFalse(body.lstrip().startswith(b"{"))
        finally:
            self.stop()

    def test_missing_attachment_is_404_not_crash(self):
        base = self.start()
        try:
            with self.assertRaises(HTTPError) as ctx:
                self.get(base + "/api/attachments/K2C-1/" + quote("нет.png"))
            self.assertEqual(ctx.exception.code, 404)
        finally:
            self.stop()

    def test_unknown_route_is_404_json(self):
        base = self.start()
        try:
            status, data = self.request(base + "/api/" + quote("какая-то-ерунда"))
            self.assertEqual(status, 404)
            self.assertIn("error", data)
        finally:
            self.stop()

    def test_commits_and_diff_endpoints(self):
        base = self.start()
        try:
            self.new_task("С коммитом")
            self.save()
            (self.repo / "a.py").write_text("print(1)\n", encoding="utf-8")
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-q", "-m", "K2C-1: коммит с хэштегом")
            status, data = self.request(base + "/api/commits?id=K2C-1")
            self.assertEqual(status, 200)
            self.assertEqual(len(data["commits"]), 1)
            rev = data["commits"][0]["hash"]
            status, diff = self.request(base + f"/api/diff?rev={rev}")
            self.assertIn("+print(1)", diff["text"])
        finally:
            self.stop()


class TestCli(TempRepoCase):
    def run_cfg(self, *args):
        """Как run_cli, но без подставного --repo: путь резолвится через конфиг."""
        return subprocess.run(["python3", str(REPO_ROOT / "board.py"), *args],
                              capture_output=True, text=True, env=self.cli_env(),
                              cwd=str(REPO_ROOT))

    def run_cli(self, *args, repo=None):
        env = self.cli_env()
        cmd = ["python3", str(REPO_ROOT / "board.py"), "--repo", str(repo or self.repo), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
        return proc

    def test_add_move_done_and_list(self):
        proc = self.run_cli("add", "Первая карточка")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "K2C-1")
        self.assertEqual(self.run_cli("move", "K2C-1", "В работе").returncode, 0)
        self.assertEqual(self.run_cli("done", "K2C-1").returncode, 0)
        listing = self.run_cli("list")
        self.assertIn("K2C-1", listing.stdout)
        self.assertEqual(self.run_cli("rm", "K2C-1", "-y").returncode, 0)

    def test_show_reports_commits_and_missing_task_exits_1(self):
        self.run_cli("add", "Карточка")
        (self.repo / "f.py").write_text("f = 1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "K2C-1: связь")
        out = self.run_cli("show", "K2C-1")
        self.assertIn("привязанные коммиты", out.stdout)
        self.assertIn("K2C-1: связь", out.stdout)
        missing = self.run_cli("show", "K2C-999")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("не найдена", missing.stderr)

    def test_module_cli(self):
        self.assertEqual(self.run_cli("module", "add", "Ядро", "--color", "#88ccee").returncode, 0)
        self.assertIn("Ядро", self.run_cli("module", "list").stdout)
        self.assertEqual(self.run_cli("add", "В ядре", "-m", "Ядро").returncode, 0)

    def test_module_rename_and_color_cli(self):
        self.run_cli("module", "add", "Ядро", "--color", "#88ccee")
        renamed = self.run_cli("module", "rename", "Ядро", "Движок")
        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        recolored = self.run_cli("module", "color", "Движок", "#77216F")
        self.assertEqual(recolored.returncode, 0, recolored.stderr)
        listed = self.run_cli("module", "list").stdout
        self.assertIn("Движок", listed)
        self.assertIn("#77216F", listed)

    def test_reinit_after_clone_keeps_data_and_fixes_repo_path(self):
        """После git clone доска уже есть: init не должен падать и не терять карточки."""
        self.run_cli("add", "Карточка из клона")
        elsewhere = self.tmp / "clone"
        elsewhere.mkdir()
        shutil.copytree(self.repo / ".board", elsewhere / ".board")
        # доска помнит путь машины, с которой её склонировали
        proc = self.run_cli("init", "--id", "K2C", "--name", "Клон", "--repo", str(elsewhere))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("перерегистрирован", proc.stdout)
        board = load_board(elsewhere)
        self.assertEqual(len(board["tasks"]), 1, "карточки не должны пропасть")
        self.assertEqual(board["project"]["repo_path"], str(elsewhere.resolve()))
        listing = self.run_cfg("--repo", str(elsewhere), "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("Карточка из клона", listing.stdout)
        # и конфиг должен указывать на клон, а не на исходную репу
        projects = self.run_cfg("projects")
        self.assertIn(str(elsewhere.resolve()), projects.stdout)
        self.assertNotIn(str(self.repo.resolve()), projects.stdout)


    def test_cli_edit_desc(self):
        """FB-23: описание переписывается флагом --desc."""
        self.run_cli("add", "Через CLI")
        proc = self.run_cli("edit", "K2C-1", "--desc", "новое описание из терминала")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"],
                         "новое описание из терминала")


    def test_cli_add_with_explicit_id(self):
        """FB-23: карточка восстанавливается с прежним номером."""
        proc = self.run_cli("add", "С номером", "--id", "42", "-d", "восстановленная")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "K2C-42")
        self.assertEqual(load_board(self.repo)["tasks"][0]["id"], "K2C-42")


    def test_cli_add_with_taken_id_refuses(self):
        self.run_cli("add", "Первая")
        dup = self.run_cli("add", "Вторая", "--id", "K2C-1")
        self.assertEqual(dup.returncode, 1)
        self.assertIn("уже есть", dup.stderr)

    def test_attach_cli_copies_file(self):
        self.run_cli("add", "Со скрином")
        src = Path(self.tmp) / "before.png"
        src.write_bytes(b"png-bytes")
        proc = self.run_cli("attach", "K2C-1", str(src), "--name", "FB-1-before.png")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FB-1-before.png", proc.stdout)
        target = storage.board_dir(self.repo) / "attachments" / "K2C-1" / "FB-1-before.png"
        self.assertEqual(target.read_bytes(), b"png-bytes")

    def test_filters_in_list(self):
        self.run_cli("module", "add", "UI")
        self.run_cli("add", "Первая", "-m", "UI")
        self.run_cli("add", "Вторая", "--session", "вечер")
        self.assertEqual(len(self.run_cli("list", "-m", "UI").stdout.strip().splitlines()), 1)
        self.assertIn("Вторая", self.run_cli("list", "--session", "вечер").stdout)
        self.assertIn("Вторая", self.run_cli("list", "-s", "вторая").stdout)
        self.assertEqual(self.run_cli("list", "-m", "нет-такого").returncode, 1)


class TestExplicitTaskId(TempRepoCase):
    """--id в add: восстановление карточки с прежним номером (FB-23)."""

    def test_normalize_accepts_both_forms(self):
        self.assertEqual(storage.normalize_task_id(self.data, "12"), "K2C-12")
        self.assertEqual(storage.normalize_task_id(self.data, "K2C-12"), "K2C-12")
        self.assertEqual(storage.normalize_task_id(self.data, "k2c-12"), "K2C-12")
        self.assertEqual(storage.normalize_task_id(self.data, "007"), "K2C-7",
                         "ведущие нули не плодят разные номера")

    def test_normalize_rejects_foreign_prefix_and_garbage(self):
        for bad in ("", "ZZZ-1", "K2C-abc", "K2C-", "просто текст"):
            with self.assertRaises(BoardError, msg=f"{bad!r} должен отбиваться"):
                storage.normalize_task_id(self.data, bad)

    def test_reserve_moves_counter_forward(self):
        taken = storage.reserve_task_id(self.data, "K2C-40")
        self.assertEqual(taken, "K2C-40")
        self.assertEqual(storage.next_task_id(self.data), "K2C-41",
                         "счётчик обязан перепрыгнуть занятый номер, иначе будет дубль")

    def test_reserve_refuses_existing(self):
        add_task(self.data, "Первая")
        with self.assertRaises(BoardError) as ctx:
            storage.reserve_task_id(self.data, "K2C-1")
        self.assertIn("уже есть", str(ctx.exception))

    def test_add_with_explicit_id_via_cli(self):
        task = add_task(self.data, "Восстановленная", explicit_id=None)
        self.assertEqual(task["id"], "K2C-1")
        again = add_task(self.data, "С номером", explicit_id="K2C-77")
        self.assertEqual(again["id"], "K2C-77")
        self.save()
        ids = [t["id"] for t in load_board(self.repo)["tasks"]]
        self.assertEqual(ids, ["K2C-1", "K2C-77"])
        self.assertEqual(add_task(load_board(self.repo), "Проверка")["id"], "K2C-78")


class TestEditDescription(TempRepoCase):
    """FB-23: описание карточки должно переписываться на месте."""

    def test_desc_flag_replaces_text(self):
        task = self.new_task("Старая", description="было")
        update_task(self.data, task["id"], {"description": "стало целиком"})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], "стало целиком")

    def test_desc_can_be_cleared(self):
        task = self.new_task("С описанием", description="что-то")
        update_task(self.data, task["id"], {"description": ""})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], "")

    def test_other_fields_do_not_touch_description(self):
        task = self.new_task("Карточка", description="описание должно уцелеть")
        update_task(self.data, task["id"], {"title": "Новое имя", "estimate": 3})
        self.save()
        fresh = load_board(self.repo)["tasks"][0]
        self.assertEqual(fresh["title"], "Новое имя")
        self.assertEqual(fresh["description"], "описание должно уцелеть")

    def test_multiline_description_survives_roundtrip(self):
        task = self.new_task("Многострочная")
        text = "Первая строка\n\n- пункт\n- пункт\n\n```\nкод\n```"
        update_task(self.data, task["id"], {"description": text})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], text)

    def test_edit_keeps_description_without_desc_flag(self):
        task = self.new_task("Заголовок", description="не трогать")
        update_task(self.data, task["id"], {"module_id": None})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], "не трогать")


class TestOrdering(TempRepoCase):
    def test_task_sort_key_orders_by_id_not_by_time(self):
        tasks = [{"id": "K2C-10"}, {"id": "K2C-2"}, {"id": "K2C-1"}]
        ordered = sorted(tasks, key=storage.task_sort_key)
        self.assertEqual([t["id"] for t in ordered], ["K2C-1", "K2C-2", "K2C-10"])

    def test_task_num(self):
        self.assertEqual(task_num("K2C-42"), 42)
        self.assertEqual(task_num("FB-7"), 7)


class TestConfig(TempRepoCase):
    """Конфиг машины: пути, проекты, приоритеты. Реестра в репе больше нет."""

    prefix = "FB"
    board_name = "FossilBoard"

    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.json"
        self.env = dict(self.cli_env(), FOSSILBOARD_CONFIG=str(self.cfg_path))

    def load(self):
        return config_mod.load(self.cfg_path)

    def test_defaults_when_file_absent(self):
        cfg = self.load()
        self.assertFalse(cfg["_exists"])
        self.assertEqual(cfg["server"]["port"], 8770)
        self.assertEqual(cfg["server"]["host"], "0.0.0.0")
        self.assertEqual(cfg["ui"]["theme"], "dark")
        self.assertEqual(cfg["projects"], [])

    def test_partial_file_keeps_other_defaults(self):
        self.cfg_path.write_text(json.dumps({"server": {"port": 9000}}), encoding="utf-8")
        cfg = self.load()
        self.assertEqual(cfg["server"]["port"], 9000)
        self.assertEqual(cfg["server"]["host"], "0.0.0.0", "остальные значения не должны теряться")
        self.assertEqual(cfg["attachments"]["max_upload_mb"], 64)

    def test_roundtrip_and_sorted_keys(self):
        cfg = self.load()
        config_mod.upsert_project(cfg, "fb", "FossilBoard", str(self.repo))
        path = config_mod.save(cfg, self.cfg_path)
        self.assertEqual(path, self.cfg_path)
        raw = self.cfg_path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw)["projects"][0]["id"], "FB", "id нормализуется в верхний регистр")
        self.assertEqual(json.loads(raw)["version"], config_mod.CONFIG_VERSION)
        keys = list(json.loads(raw))
        self.assertEqual(keys, sorted(keys), "ключи конфига тоже сортированы ради diff")

    def test_broken_config_reports_and_keeps_working(self):
        """Битый конфиг не должен превращаться в отказ работать: доска живёт
        на значениях по умолчанию и говорит, что поправить."""
        self.cfg_path.write_text("{ не json", encoding="utf-8")
        with self.assertRaises(config_mod.ConfigError) as ctx:
            self.load()
        self.assertIn("битый JSON", str(ctx.exception))
        # значения по умолчанию остаются целыми и после неудачной загрузки
        self.assertEqual(config_mod.defaults()["server"]["port"], 8770)
        self.assertEqual(config_mod.defaults()["projects"], [])
        self.assertEqual(config_mod.defaults(self.cfg_path)["_path"], str(self.cfg_path))
        # CLI перехватывает ошибку и продолжает работать
        proc = self.run_cfg("list")
        self.assertEqual(proc.returncode, 1, "без проектов в конфиге это честная ошибка")
        self.assertIn("битый JSON", proc.stderr)
        # но с явным --repo доска работает как обычно — битый конфиг не блокирует работу
        self.run_cfg("--repo", str(self.repo), "add", "Задача до битого конфига")
        ok = self.run_cfg("--repo", str(self.repo), "list")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("битый JSON", ok.stderr)
        self.assertIn("Задача до битого конфига", ok.stdout)

    def run_cfg(self, *args):
        return subprocess.run(["python3", str(REPO_ROOT / "board.py"), *args],
                              capture_output=True, text=True, env=self.env, cwd=str(REPO_ROOT))

    def test_priority_cli_over_env_over_config(self):
        self.cfg_path.write_text(json.dumps({"server": {"host": "127.0.0.1", "port": 9100}}),
                                 encoding="utf-8")
        cfg = self.load()
        host, port = config_mod.effective_server(cfg, None, None)
        self.assertEqual((host, port), ("127.0.0.1", 9100), "из конфига")
        with mock.patch.dict(os.environ, {"FOSSILBOARD_PORT": "9200"}):
            host, port = config_mod.effective_server(cfg, None, None)
            self.assertEqual(port, 9200, "переменная окружения перекрывает конфиг")
            host, port = config_mod.effective_server(cfg, "0.0.0.0", 9300)
            self.assertEqual((host, port), ("0.0.0.0", 9300), "флаг CLI перекрывает всё")

    def test_resolve_repo_chain(self):
        other = self.tmp / "other"
        other.mkdir()
        cfg = self.load()
        config_mod.upsert_project(cfg, "FB", "FossilBoard", str(self.repo))
        config_mod.upsert_project(cfg, "K2C", "Kingdom-like", str(other))
        # --repo сильнее всего
        self.assertEqual(config_mod.resolve_repo(cfg, str(other)), str(other.resolve()))
        # явный проект
        self.assertEqual(config_mod.resolve_repo(cfg, None, "k2c"), str(other.resolve()))
        # без указаний при нескольких проектах — ничего не выдумываем
        self.assertIsNone(config_mod.resolve_repo(cfg))
        # единственный проект — очевидный выбор
        cfg["projects"] = [cfg["projects"][0]]
        self.assertEqual(config_mod.resolve_repo(cfg), str(self.repo.resolve()))

    def test_upsert_updates_instead_of_duplicating(self):
        cfg = self.load()
        first = config_mod.upsert_project(cfg, "FB", "FossilBoard", str(self.repo))
        again = config_mod.upsert_project(cfg, "fb", "Другое имя", str(self.tmp))
        self.assertEqual(len(cfg["projects"]), 1, "повторный init не должен плодить проекты")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["name"], "Другое имя")
        self.assertTrue(config_mod.remove_project(cfg, "FB"))
        self.assertFalse(config_mod.remove_project(cfg, "FB"))

    def test_migrate_legacy_registry_from_repo(self):
        """Проекты из старого .board/projects.json переезжают в конфиг один раз."""
        legacy = self.repo / ".board" / "projects.json"
        legacy.write_text(json.dumps({"projects": [
            {"id": "FB", "name": "FossilBoard", "repo_path": str(self.repo)}]}), encoding="utf-8")
        cfg = self.load()
        moved = config_mod.migrate_legacy_registry(cfg, self.cfg_path,
                                                   candidates=[legacy])
        self.assertEqual(moved, ["FB"])
        self.assertFalse(legacy.exists(), "реестр из репозитория удаляется")
        config_mod.save(cfg, self.cfg_path)
        again = config_mod.migrate_legacy_registry(self.load(), self.cfg_path,
                                                   candidates=[legacy])
        self.assertEqual(again, [], "повторная миграция не должна ничего трогать")

    def test_upload_limit_from_config(self):
        from fossilboard.server import upload_limit
        cfg = self.load()
        self.assertEqual(upload_limit(cfg), 64 * 1024 * 1024)
        cfg = config_mod.set_value(cfg, "attachments.max_upload_mb", 8)
        self.assertEqual(upload_limit(cfg), 8 * 1024 * 1024)


class TestCliConfig(TempRepoCase):
    """Команды board.py config — то, чем пользуется человек, а не тест."""

    prefix = "FB"
    board_name = "FossilBoard"

    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.json"
        self.env = dict(self.cli_env(), FOSSILBOARD_CONFIG=str(self.cfg_path))

    def run_cfg(self, *args):
        env = dict(self.env)
        proc = subprocess.run(["python3", str(REPO_ROOT / "board.py"), *args],
                              capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
        return proc

    def test_init_writes_config_and_projects_lists_it(self):
        proc = self.run_cfg("init", "--id", "FB", "--name", "FossilBoard", "--repo", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.cfg_path.is_file())
        listed = self.run_cfg("projects")
        self.assertIn("FB", listed.stdout)
        self.assertIn(str(self.repo), listed.stdout)
        self.assertIn("[✓]", listed.stdout, "отмечаем, что доска на месте")

    def test_config_show_path_and_set(self):
        self.run_cfg("init", "--id", "FB", "--name", "FossilBoard", "--repo", str(self.repo))
        self.assertEqual(self.run_cfg("config", "path").stdout.strip(), str(self.cfg_path))
        shown = self.run_cfg("config")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("server", shown.stdout)
        changed = self.run_cfg("config", "set", "server.port", "9111")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertIn("9111", changed.stdout)
        self.assertEqual(json.loads(self.cfg_path.read_text())["server"]["port"], 9111)

    def test_config_add_and_rm_project(self):
        added = self.run_cfg("config", "add-project", "K2C", "Kingdom-like", "--repo", str(self.repo))
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("конфиг:", added.stdout)
        self.assertIn("K2C", self.run_cfg("projects").stdout)
        self.assertEqual(self.run_cfg("config", "rm-project", "K2C").returncode, 0)
        self.assertNotIn("K2C", self.run_cfg("projects").stdout)
        self.assertEqual(self.run_cfg("config", "rm-project", "K2C").returncode, 1)

    def test_serve_uses_config_port(self):
        """Порт для serve берётся из конфига, флаг CLI его перекрывает."""
        self.run_cfg("init", "--id", "FB", "--name", "FossilBoard", "--repo", str(self.repo))
        self.run_cfg("config", "set", "server.port", "8999")

        from fossilboard import cli as cli_mod
        from fossilboard import server as server_mod
        started = {}

        def fake_serve(repo=None, host=None, port=None, cfg=None):
            started.update(repo=repo, host=host, port=port, cfg=cfg)
            return 0

        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(server_mod, "serve", fake_serve):
            self.assertEqual(cli_mod.main(["serve"]), 0)
            self.assertEqual(started.get("port"), 8999, "порт должен прийти из конфига")
            self.assertEqual(started.get("host"), "0.0.0.0")
            self.assertEqual(started.get("repo"), str(self.repo.resolve()))

            self.assertEqual(cli_mod.main(["serve", "--port", "8123"]), 0)
            self.assertEqual(started.get("port"), 8123, "флаг CLI перекрывает конфиг")

            os.environ["FOSSILBOARD_PORT"] = "8777"
            try:
                self.assertEqual(cli_mod.main(["serve"]), 0)
                self.assertEqual(started.get("port"), 8777, "переменная окружения перекрывает конфиг")
            finally:
                del os.environ["FOSSILBOARD_PORT"]


    def test_serve_starts_with_several_projects(self):
        """Серверу не нужен проект по умолчанию: он показывает их все."""
        second = self.tmp / "second"
        second.mkdir()
        git(second, "init", "-q")
        init_board(second, "SEC", "Второй проект")
        # оба проекта должны быть в конфиге — иначе «единственный очевидный»
        # выбор скроет проверку
        self.run_cfg("config", "add-project", "FB", "FossilBoard", "--repo", str(self.repo))
        self.run_cfg("config", "add-project", "SEC", "Второй проект", "--repo", str(second))

        from fossilboard import cli as cli_mod
        from fossilboard import server as server_mod
        started = {}

        def fake_serve(repo=None, host=None, port=None, cfg=None):
            started.update(repo=repo, cfg=cfg)
            return 0

        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(server_mod, "serve", fake_serve):
            code = cli_mod.main(["serve"])
        self.assertEqual(code, 0, "serve не должен падать из-за нескольких проектов")
        self.assertIsNone(started.get("repo"),
                          "без явного выбора сервер работает со всеми проектами")
        self.assertEqual(len(started["cfg"]["projects"]), 2)

    def test_projects_add_creates_board_when_missing(self):
        """projects add — человеческий вход: сам создаёт доску, если её нет."""
        repo = self.tmp / "demo"
        repo.mkdir()
        git(repo, "init", "-q")
        proc = self.run_cfg("projects", "add", "DEMO", "Демо", "--repo", str(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("доска будет создана", proc.stdout)
        board = load_board(repo)
        self.assertEqual(board["project"]["id"], "DEMO")
        self.assertEqual(board["project"]["repo_path"], str(repo.resolve()))
        self.assertIn("DEMO", self.run_cfg("projects").stdout)

    def test_projects_add_keeps_existing_board(self):
        self.run_cfg("projects", "add", "FB", "Переименованное", "--repo", str(self.repo))
        self.assertEqual(load_board(self.repo)["tasks"], [], "доска не пересоздаётся")
        listed = self.run_cfg("projects").stdout
        self.assertIn("FB", listed)
        self.assertIn("Переименованное", listed)

    def test_projects_rm_and_show(self):
        self.run_cfg("projects", "add", "DEMO", "Демо", "--repo", str(self.tmp))
        shown = self.run_cfg("projects", "show", "DEMO")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("repo_path", shown.stdout)
        self.assertEqual(self.run_cfg("projects", "rm", "DEMO").returncode, 0)
        self.assertEqual(self.run_cfg("projects", "rm", "DEMO").returncode, 1)
        self.assertEqual(self.run_cfg("projects", "show", "DEMO").returncode, 1)


class TestConfigLookup(TempRepoCase):
    """Где доска ищет конфиг: env → ./config.json → ~/.config → умолчания."""

    prefix = "FB"
    board_name = "FossilBoard"

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "project"
        self.work.mkdir()

    def test_template_ships_with_the_repo(self):
        tpl = config_mod.template_path()
        self.assertTrue(tpl.is_file(), f"шаблон должен лежать в репозитории: {tpl}")
        data = json.loads(tpl.read_text(encoding="utf-8"))
        self.assertIn("server", data)
        self.assertIn("_comment", data, "в шаблоне должны быть пояснения")

    def test_local_config_wins_over_home(self):
        # страховка фикстуры ставит FOSSILBOARD_CONFIG — для проверки порядка
        # поиска его надо убрать, иначе верхним будет именно он
        saved = os.environ.pop("FOSSILBOARD_CONFIG", None)
        try:
            self._check_local_vs_home()
        finally:
            if saved is not None:
                os.environ["FOSSILBOARD_CONFIG"] = saved

    def _check_local_vs_home(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")}, clear=False):
            home = config_mod.user_config_path()
            home.parent.mkdir(parents=True, exist_ok=True)
            home.write_text(json.dumps({"server": {"port": 1111}}), encoding="utf-8")
            local = self.work / "config.json"
            local.write_text(json.dumps({"server": {"port": 2222}}), encoding="utf-8")
            with mock.patch.object(Path, "cwd", return_value=self.work):
                self.assertEqual(config_mod.config_path(), local.resolve(),
                                 "конфиг рядом с доской важнее общесистемного")
            local.unlink()
            with mock.patch.object(Path, "cwd", return_value=self.work):
                self.assertEqual(config_mod.config_path(), home,
                                 "без локального берётся общесистемный")

    def test_env_path_wins_over_everything(self):
        custom = self.tmp / "custom.json"
        custom.write_text(json.dumps({"server": {"port": 3333}}), encoding="utf-8")
        local = self.work / "config.json"
        local.write_text(json.dumps({"server": {"port": 2222}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FOSSILBOARD_CONFIG": str(custom)}):
            with mock.patch.object(Path, "cwd", return_value=self.work):
                self.assertEqual(config_mod.config_path(), custom.resolve())

    def test_comments_from_template_do_not_leak_into_data(self):
        raw = config_mod.template_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        data["projects"] = []          # шаблон содержит пример проекта
        merged = config_mod._deep_merge(config_mod.defaults(), data)
        # в данных остаются только наши служебные ключи, комментарии не проходят
        self.assertEqual([k for k in merged if k.startswith("_")], ["_path", "_exists"])
        self.assertNotIn("_comment", merged)
        self.assertEqual(merged["server"]["port"], 8770)
        # и в записанный конфиг подчёркнутые ключи тоже не попадают
        saved = {k: v for k, v in merged.items() if not k.startswith("_")}
        self.assertEqual(sorted(saved), ["attachments", "default_project", "flags", "git",
                                        "paths", "projects", "server", "ui", "version"])
        self.assertTrue([k for k in data if k.startswith("_")],
                        "в шаблоне есть комментарии")
        for key in data:
            if key.startswith("_"):
                self.assertNotIn(key, saved, f"комментарий {key} не должен попасть в данные")

    def test_config_init_writes_local_file_from_values(self):
        env = dict(self.cli_env(), FOSSILBOARD_CONFIG=str(self.tmp / "empty.json"))
        proc = subprocess.run(["python3", str(REPO_ROOT / "board.py"), "config", "init"],
                              capture_output=True, text=True, env=env, cwd=str(self.work))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        created = self.work / "config.json"
        self.assertTrue(created.is_file())
        self.assertIn("конфиг создан", proc.stdout)
        self.assertIn(str(created), proc.stdout)
        self.assertEqual(json.loads(created.read_text(encoding="utf-8"))["server"]["port"], 8770)
        # второй раз не затираем молча
        again = subprocess.run(["python3", str(REPO_ROOT / "board.py"), "config", "init"],
                               capture_output=True, text=True, env=env, cwd=str(self.work))
        self.assertEqual(again.returncode, 1)
        self.assertIn("уже есть", again.stderr)


class TestRepoManager(TempRepoCase):
    """Создание и подключение реп из интерфейса: пути руками, ничего не стираем."""

    prefix = "FB"
    board_name = "FossilBoard"

    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.json"
        self.cfg = config_mod.load(self.cfg_path)
        config_mod.upsert_project(self.cfg, "FB", "FossilBoard", str(self.repo))

    def test_expand_requires_absolute_path(self):
        with self.assertRaises(repolib.RepoError):
            repolib.expand("projects/relative")
        with self.assertRaises(repolib.RepoError):
            repolib.expand("")
        self.assertEqual(repolib.expand(str(self.repo)), self.repo.resolve())
        self.assertTrue(str(repolib.expand("~/x")).startswith(str(Path.home())))

    def test_create_makes_folder_git_and_board(self):
        target = self.tmp / "new-game"
        result = repolib.attach(self.cfg, str(target), project_id="GAME", name="New Game",
                                create=True)
        self.assertTrue((target / ".git").is_dir(), "репа должна быть git")
        self.assertTrue(storage.board_path(target).is_file())
        self.assertEqual(result["project"]["id"], "GAME")
        self.assertEqual(result["project"]["repo_path"], str(target.resolve()))
        self.assertEqual(config_mod.load(self.cfg_path)["projects"][-1]["id"], "GAME")
        board = load_board(target)
        self.assertEqual(board["project"]["repo_path"], str(target.resolve()))

    def test_create_in_existing_folder_keeps_files(self):
        target = self.tmp / "existing"
        target.mkdir()
        (target / "main.py").write_text("мой код\n", encoding="utf-8")
        repolib.attach(self.cfg, str(target), project_id="EXST", create=True)
        self.assertEqual((target / "main.py").read_text(encoding="utf-8"), "мой код\n")
        self.assertTrue(storage.board_path(target).is_file())

    def test_create_twice_is_a_clear_error(self):
        target = self.tmp / "twice"
        repolib.attach(self.cfg, str(target), project_id="TW", create=True)
        with self.assertRaises(repolib.RepoError) as ctx:
            repolib.attach(self.cfg, str(target), project_id="TW", create=True)
        self.assertIn("уже есть", str(ctx.exception))

    def test_open_existing_repo_fixes_stale_path(self):
        stale = load_board(self.repo)
        stale["project"]["repo_path"] = "/где/то/на/другой/машине"
        save_board(self.repo, stale)
        result = repolib.attach(self.cfg, str(self.repo), create=False)
        self.assertEqual(result["project"]["repo_path"], str(self.repo.resolve()))
        self.assertEqual(load_board(self.repo)["project"]["repo_path"], str(self.repo.resolve()))

    def test_open_refuses_non_git_and_boardless(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(repolib.RepoError) as ctx:
            repolib.attach(self.cfg, str(plain), create=False)
        self.assertIn("не git-репозиторий", str(ctx.exception))

        nopanel = self.tmp / "nopanel"
        nopanel.mkdir()
        git(nopanel, "init", "-q")
        with self.assertRaises(repolib.RepoError) as ctx:
            repolib.attach(self.cfg, str(nopanel), create=False)
        self.assertIn("нет доски", str(ctx.exception))
        self.assertIn("board.py init", str(ctx.exception), "подсказка с командой обязательна")

    def test_open_boardless_repo_creates_board_when_prefix_given(self):
        """Репу без доски можно открыть и сразу завести доску прямо из диалога."""
        nopanel = self.tmp / "rezka"
        nopanel.mkdir()
        git(nopanel, "init", "-q")
        (nopanel / "main.py").write_text("код\n", encoding="utf-8")

        result = repolib.attach(self.cfg, str(nopanel), project_id="RZ",
                                name="Резка", create=False)
        self.assertEqual(result["project"]["id"], "RZ")
        self.assertEqual(result["project"]["name"], "Резка")
        board = load_board(nopanel)
        self.assertEqual(board["project"]["id"], "RZ")
        self.assertEqual(board["project"]["repo_path"], str(nopanel.resolve()))
        self.assertEqual((nopanel / "main.py").read_text(encoding="utf-8"), "код\n",
                         "чужие файлы репы не трогаем")

    def test_existing_board_wins_over_typed_prefix(self):
        """Если доска уже есть, введённый префикс её не переименовывает."""
        stale = load_board(self.repo)
        keep = stale["project"]["id"]
        repolib.attach(self.cfg, str(self.repo), project_id="ЧТОТО", name="Другое",
                       create=False)
        self.assertEqual(load_board(self.repo)["project"]["id"], keep)
        self.assertEqual(config_mod.load(self.cfg_path)["projects"][0]["id"], keep)

    def test_open_missing_path_refused(self):
        with self.assertRaises(repolib.RepoError) as ctx:
            repolib.attach(self.cfg, str(self.tmp / "нет-такого"), create=False)
        self.assertIn("нет такого каталога", str(ctx.exception))

    def test_suggest_id_from_folder_name(self):
        self.assertEqual(repolib.suggest_id(Path("/home/u/projects/k2c-proto"), []), "K2CP")
        busy = repolib.suggest_id(Path("/home/u/projects/game"), ["GAME"])
        self.assertNotEqual(busy, "GAME", "занятый префикс надо развести")

    def test_describe_reports_state_for_ui(self):
        info = repolib.describe(self.repo)
        self.assertTrue(info["is_dir"] and info["is_git"] and info["has_board"])
        self.assertTrue(info["board_ok"])
        storage.board_path(self.repo).write_text("{ сломан", encoding="utf-8")
        broken = repolib.describe(self.repo)
        self.assertFalse(broken["board_ok"])
        self.assertIn("битый JSON", broken["board_error"])
        missing = repolib.describe(self.tmp / "нет")
        self.assertFalse(missing["exists"])

    def test_browse_skips_build_noise(self):
        for noise in ("__pycache__", "node_modules", "build", "venv"):
            (self.tmp / noise).mkdir()
        (self.tmp / "k2c-proto").mkdir()
        names = [d["name"] for d in repolib.browse(str(self.tmp))[0]["dirs"]]
        self.assertIn("k2c-proto", names)
        for noise in ("__pycache__", "node_modules", "build", "venv"):
            self.assertNotIn(noise, names, f"служебный каталог {noise} в подсказке — мусор")

    def test_browse_shows_contents_of_the_typed_directory(self):
        """Подсказки — это содержимое набранного каталога, как в файловом менеджерере."""
        inside = self.tmp / "games"
        inside.mkdir()
        (inside / "k2c").mkdir()
        (self.tmp / "other").mkdir()

        data = repolib.browse(str(inside))[0]
        self.assertEqual([d["name"] for d in data["dirs"]], ["k2c"])
        self.assertEqual(data["self"], str(inside), "нужно знать, где мы находимся")
        self.assertEqual(data["parent"], str(self.tmp), "и куда можно подняться")

    def test_browse_falls_back_to_parent_for_missing_path(self):
        inside = self.tmp / "games"
        inside.mkdir()
        (inside / "k2c").mkdir()
        missing = self.tmp / "games" / "k2c-pro"
        data = repolib.browse(str(missing))[0]
        self.assertEqual(data["self"], str(inside))
        self.assertIn("k2c", [d["name"] for d in data["dirs"]])

    def test_browse_keeps_root_sane(self):
        """От «/» не уходим в бесконечность: родитель остаётся корнем."""
        data = repolib.browse("/")[0]
        self.assertEqual(data["parent"], "/")
        self.assertEqual(data["self"], "/")

    def test_suggest_entry_carries_name(self):
        """Подсказка отдаёт имя каталога, чтобы заменять им последний сегмент."""
        (self.tmp / "rezka-miner").mkdir()
        dirs = repolib.browse(str(self.tmp))[0]["dirs"]
        entry = next(d for d in dirs if d["name"] == "rezka-miner")
        self.assertEqual(entry["name"], "rezka-miner")
        self.assertEqual(entry["path"], str(self.tmp / "rezka-miner"))


class TestRepoApi(TempRepoCase):
    """Те же сценарии через HTTP — как их зовёт браузер."""

    prefix = "FB"
    board_name = "FossilBoard"

    @classmethod
    def setUpClass(cls):
        from fossilboard.server import Handler
        cls.Handler = Handler

    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.json"
        cfg = config_mod.load(self.cfg_path)
        config_mod.upsert_project(cfg, "FB", "FossilBoard", str(self.repo))
        config_mod.save(cfg, self.cfg_path)
        self.Handler.default_repo = str(self.repo)
        self.Handler.cfg = config_mod.load(self.cfg_path)
        self.Handler.quiet = True
        # сервер перечитывает конфиг по требованию — значит, из окружения теста,
        # а не из боевого ~/.config/fossilboard
        self._env = mock.patch.dict(os.environ, {"FOSSILBOARD_CONFIG": str(self.cfg_path)})
        self._env.start()
        self.Handler.cfg = None  # пусть перечитает из своего окружения
        import threading
        from http.server import ThreadingHTTPServer
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self._env.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, url):
        with urlopen(url) as resp:
            return resp.status, json.loads(resp.read())

    def post(self, url, payload):
        req = Request(url, data=json.dumps(payload).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_create_and_open_through_api(self):
        target = self.tmp / "api-game"
        status, data = self.post(self.base + "/api/projects",
                                 {"path": str(target), "create": True, "project_id": "APIG"})
        self.assertEqual(status, 201, data)
        self.assertTrue(storage.board_path(target).is_file())
        # проект появился в состоянии доски без перезапуска сервера
        status, state = self.get(self.base + "/api/state")
        self.assertIn("APIG", [p["id"] for p in state["projects"]])
        # и его доска читается
        status, board = self.get(self.base + f"/api/board?project=APIG")
        self.assertEqual(board["project"]["id"], "APIG")

    def test_api_refuses_relative_path(self):
        status, data = self.post(self.base + "/api/projects", {"path": "relative/path"})
        self.assertEqual(status, 400)
        self.assertIn("абсолютный путь", data["error"])

    def test_api_refuses_non_repo_with_hint(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        status, data = self.post(self.base + "/api/projects", {"path": str(plain)})
        self.assertEqual(status, 400)
        self.assertIn("не git-репозиторий", data["error"])

    def test_api_creates_board_in_boardless_repo(self):
        """Сценарий из интерфейса: открываете репу без доски и жмёте «создать»."""
        target = self.tmp / "rezka-miner"
        target.mkdir()
        git(target, "init", "-q")
        status, data = self.post(self.base + "/api/projects",
                                 {"path": str(target), "create": False,
                                  "project_id": "RZ", "name": "Резка"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["project"]["id"], "RZ")
        self.assertTrue(data["board_created"], "интерфейс должен знать, что доска создана")
        self.assertTrue(storage.board_path(target).is_file())

    def test_api_reports_board_not_created_when_opening_ready_repo(self):
        status, data = self.post(self.base + "/api/projects", {"path": str(self.repo)})
        self.assertEqual(status, 201, data)
        self.assertFalse(data["board_created"])

    def test_api_without_prefix_keeps_helpful_error(self):
        target = self.tmp / "no-board"
        target.mkdir()
        git(target, "init", "-q")
        status, data = self.post(self.base + "/api/projects", {"path": str(target)})
        self.assertEqual(status, 400)
        self.assertIn("board.py init", data["error"])

    def test_config_changes_are_picked_up_without_restart(self):
        """Проект, добавленный в конфиг при работающей доске, должен появляться.

        Регрессия (FB-23): сервер держал копию конфига в памяти, и проект,
        добавленный из CLI или из другой сессии, был не виден до перезапуска.
        """
        second = self.tmp / "miner"
        second.mkdir()
        git(second, "init", "-q")
        init_board(second, "MINER", "FossilMiner")

        _, before = self.get(self.base + "/api/state")
        self.assertNotIn("MINER", [p["id"] for p in before["projects"]])

        cfg = config_mod.load(self.cfg_path)
        config_mod.upsert_project(cfg, "MINER", "FossilMiner", str(second))
        config_mod.save(cfg, self.cfg_path)

        _, after = self.get(self.base + "/api/state")
        self.assertIn("MINER", [p["id"] for p in after["projects"]],
                      "конфиг перечитывается, а не кэшируется навсегда")
        _, board = self.get(self.base + "/api/board?project=MINER")
        self.assertEqual(board["project"]["id"], "MINER")

    def test_removed_project_disappears_from_state(self):
        cfg = config_mod.load(self.cfg_path)
        config_mod.upsert_project(cfg, "GONE", "Исчезающий", str(self.tmp))
        config_mod.save(cfg, self.cfg_path)
        _, state = self.get(self.base + "/api/state")
        self.assertIn("GONE", [p["id"] for p in state["projects"]])
        config_mod.remove_project(cfg, "GONE")
        config_mod.save(cfg, self.cfg_path)
        _, state = self.get(self.base + "/api/state")
        self.assertNotIn("GONE", [p["id"] for p in state["projects"]])

    def test_browse_endpoint(self):
        (self.tmp / "idea").mkdir()
        status, data = self.get(self.base + f"/api/browse?path={quote(str(self.tmp))}")
        self.assertEqual(status, 200)
        self.assertIn("idea", [d["name"] for d in data[0]["dirs"]])

    def test_repo_probe_endpoint(self):
        status, data = self.get(self.base + f"/api/repo?path={quote(str(self.repo))}")
        self.assertEqual(status, 200)
        self.assertTrue(data["has_board"])
        self.assertTrue(data["is_git"])

    def test_fail_endpoint_records_attempt_without_moving_card(self):
        task = add_task(self.data, "Задача")
        move_task(self.data, task["id"], "wip")
        save_board(self.repo, self.data)
        status, data = self.post(
            self.base + f"/api/tasks/{task['id']}/fail",
            {"text": "пробовала X", "why": "не взлетело", "tags": ["tk"]})
        self.assertEqual(status, 201)
        self.assertEqual(data["attempt"]["text"], "пробовала X")
        self.assertEqual(data["task"]["column_id"], "wip", "карточка осталась на месте")
        self.assertEqual(len(data["task"]["attempts"]), 1)
        _, board = self.get(self.base + "/api/board")
        card = [t for t in board["tasks"] if t["id"] == task["id"]][0]
        self.assertEqual(len(card["attempts"]), 1, "запись видна и в списке доски")

    def test_fail_endpoint_rejects_empty_text(self):
        task = add_task(self.data, "Задача")
        save_board(self.repo, self.data)
        status, data = self.post(self.base + f"/api/tasks/{task['id']}/fail", {"text": "  "})
        self.assertEqual(status, 400)
        self.assertIn("error", data)


class TestWebMarkup(TempRepoCase):
    """Статика отдаётся как есть: проверяем, что в разметке нет ловушек."""

    def test_no_duplicate_ids(self):
        """Дубль id ломает $(...) молча: элемент перестаёт получать события.

        Так случилось с #repo-path: он был и в шапке, и в диалоге новой репы,
        и весь диалог писал в шапку вместо поля ввода.
        """
        html = (REPO_ROOT / "fossilboard" / "web" / "index.html").read_text(encoding="utf-8")
        ids = re.findall(r'\bid="([^"]+)"', html)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dupes, [], f"повторяющиеся id в index.html: {dupes}")

    def test_selectors_used_by_js_exist_in_markup(self):
        html = (REPO_ROOT / "fossilboard" / "web" / "index.html").read_text(encoding="utf-8")
        app = (REPO_ROOT / "fossilboard" / "web" / "app.js").read_text(encoding="utf-8")
        used = set(re.findall(r"\$\('#([a-zA-Z0-9_-]+)'\)", app))
        missing = sorted(i for i in used if f'id="{i}"' not in html)
        self.assertEqual(missing, [], f"app.js обращается к несуществующим элементам: {missing}")

    def test_hidden_attribute_is_not_overridden_by_display(self):
        """Скрытие через hidden должно реально работать, и поля репы не прячутся.

        display:flex у .repo-field перебивал атрибут hidden — поля «префикс» и
        «имя» оставались видимыми. Их прятать перестали намеренно: при открытии
        репы без доски доска досоздаётся прямо из диалога, так что поля нужны
        в обоих режимах.
        """
        css = (REPO_ROOT / "fossilboard" / "web" / "style.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important")
        app = (REPO_ROOT / "fossilboard" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn(".hidden = !create", app,
                         "поля префикса и имени нужны в обоих режимах")
        self.assertIn("closest('.repo-field').hidden = false", app)

    def test_cancel_button_is_not_swallowed(self):
        """«Отмена» закрывает диалог: submit с value=cancel перехватывать нельзя."""
        app = (REPO_ROOT / "fossilboard" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("e.submitter.value === 'cancel'", app)
        handler = app.split("$('#repo-form').addEventListener('submit'")[1][:600]
        self.assertLess(handler.index("return;"), handler.index("e.preventDefault()"),
                        "проверка отмены должна идти до preventDefault")

    def test_no_remote_resources(self):
        """Доска работает офлайн: внешних загрузок в статике быть не должно."""
        web = REPO_ROOT / "fossilboard" / "web"
        for path in web.iterdir():
            if path.suffix not in (".html", ".js", ".css"):
                continue
            text = path.read_text(encoding="utf-8")
            for url in re.findall(r'(?:src|href)=["\'](https?://[^"\']+)', text):
                self.fail(f"{path.name} тянет {url}")
        html = (web / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("cdn.", html)
        self.assertNotIn("fonts.googleapis", html)


class TestExplicitTaskId(TempRepoCase):
    """--id в add: восстановление карточки с прежним номером (FB-23)."""

    def test_normalize_accepts_both_forms(self):
        self.assertEqual(storage.normalize_task_id(self.data, "12"), "K2C-12")
        self.assertEqual(storage.normalize_task_id(self.data, "K2C-12"), "K2C-12")
        self.assertEqual(storage.normalize_task_id(self.data, "k2c-12"), "K2C-12")
        self.assertEqual(storage.normalize_task_id(self.data, "007"), "K2C-7",
                         "ведущие нули не плодят разные номера")

    def test_normalize_rejects_foreign_prefix_and_garbage(self):
        for bad in ("", "ZZZ-1", "K2C-abc", "K2C-", "просто текст"):
            with self.assertRaises(BoardError, msg=f"{bad!r} должен отбиваться"):
                storage.normalize_task_id(self.data, bad)

    def test_reserve_moves_counter_forward(self):
        taken = storage.reserve_task_id(self.data, "K2C-40")
        self.assertEqual(taken, "K2C-40")
        self.assertEqual(storage.next_task_id(self.data), "K2C-41",
                         "счётчик обязан перепрыгнуть занятый номер, иначе будет дубль")

    def test_reserve_refuses_existing(self):
        add_task(self.data, "Первая")
        with self.assertRaises(BoardError) as ctx:
            storage.reserve_task_id(self.data, "K2C-1")
        self.assertIn("уже есть", str(ctx.exception))

    def test_add_with_explicit_id_via_cli(self):
        task = add_task(self.data, "Восстановленная", explicit_id=None)
        self.assertEqual(task["id"], "K2C-1")
        again = add_task(self.data, "С номером", explicit_id="K2C-77")
        self.assertEqual(again["id"], "K2C-77")
        self.save()
        ids = [t["id"] for t in load_board(self.repo)["tasks"]]
        self.assertEqual(ids, ["K2C-1", "K2C-77"])
        self.assertEqual(add_task(load_board(self.repo), "Проверка")["id"], "K2C-78")


class TestEditDescription(TempRepoCase):
    """FB-23: описание карточки должно переписываться на месте."""

    def test_desc_flag_replaces_text(self):
        task = self.new_task("Старая", description="было")
        update_task(self.data, task["id"], {"description": "стало целиком"})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], "стало целиком")

    def test_desc_can_be_cleared(self):
        task = self.new_task("С описанием", description="что-то")
        update_task(self.data, task["id"], {"description": ""})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], "")

    def test_other_fields_do_not_touch_description(self):
        task = self.new_task("Карточка", description="описание должно уцелеть")
        update_task(self.data, task["id"], {"title": "Новое имя", "estimate": 3})
        self.save()
        fresh = load_board(self.repo)["tasks"][0]
        self.assertEqual(fresh["title"], "Новое имя")
        self.assertEqual(fresh["description"], "описание должно уцелеть")

    def test_multiline_description_survives_roundtrip(self):
        task = self.new_task("Многострочная")
        text = "Первая строка\n\n- пункт\n- пункт\n\n```\nкод\n```"
        update_task(self.data, task["id"], {"description": text})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], text)

    def test_edit_keeps_description_without_desc_flag(self):
        task = self.new_task("Заголовок", description="не трогать")
        update_task(self.data, task["id"], {"module_id": None})
        self.save()
        self.assertEqual(load_board(self.repo)["tasks"][0]["description"], "не трогать")


class TestGotchasAndAttempts(TempRepoCase):
    """FB-905: у карточки свои поля под грабли и неудачные пути.

    Описание карточки пишут руками, автоматика пишет в поля — иначе
    человек и машина правят один хвост и теряют правки.
    """

    def run_cli(self, *args):
        env = self.cli_env()
        cmd = ["python3", str(REPO_ROOT / "board.py"), "--repo", str(self.repo), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))

    def test_gotcha_lives_in_its_own_field(self):
        task = self.new_task("Задача", description="текст человека")
        storage.add_gotcha(task, "Tk не thread-safe", flag="#rake", source="commit")
        self.save()
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(fresh["description"], "текст человека", "описание человек не трогаем")
        self.assertEqual(len(fresh["gotchas"]), 1)
        gotcha = fresh["gotchas"][0]
        self.assertEqual(gotcha["text"], "Tk не thread-safe")
        self.assertEqual(gotcha["flag"], "rake", "решётка убирается, метка остаётся")
        self.assertEqual(gotcha["source"], "commit")

    def test_empty_gotcha_is_rejected(self):
        task = self.new_task("X")
        with self.assertRaises(BoardError):
            storage.add_gotcha(task, "   ")

    def test_gotcha_limit_keeps_card_readable(self):
        task = self.new_task("X")
        for i in range(storage.MAX_GOTCHAS):
            storage.add_gotcha(task, f"грабля {i}")
        with self.assertRaises(BoardError):
            storage.add_gotcha(task, "ещё одна")

    def test_attempt_keeps_status_and_description(self):
        task = self.new_task("X", description="описание")
        move_task(self.data, task["id"], "wip")
        storage.add_attempt(task, "пробовала X", why="не взлетело",
                            tried="libtk8.6", tags=["tk", " tk "])
        self.save()
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(fresh["column_id"], "wip", "неудачный путь не закрывает карточку")
        self.assertEqual(fresh["description"], "описание")
        attempt = fresh["attempts"][0]
        self.assertEqual(attempt["why"], "не взлетело")
        self.assertEqual(attempt["tried"], "libtk8.6")
        self.assertEqual(attempt["tags"], ["tk"], "пустые метки выбрасываются")

    def test_old_task_without_fields_loads(self):
        task = self.new_task("Старая")
        self.save()
        path = storage.board_path(self.repo)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0].pop("gotchas", None)
        data["tasks"][0].pop("attempts", None)
        data["schema_version"] = 1
        storage.atomic_write_json(path, data)
        fresh = load_board(self.repo)
        self.assertEqual(storage.gotchas_of(fresh["tasks"][0]), [])
        self.assertEqual(storage.attempts_of(fresh["tasks"][0]), [])

    def test_new_schema_version_is_written(self):
        self.save()
        self.assertEqual(load_board(self.repo)["schema_version"], 2)

    def test_newer_schema_version_gives_clear_error(self):
        self.save()
        path = storage.board_path(self.repo)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["schema_version"] = 99
        storage.atomic_write_json(path, data)
        with self.assertRaises(BoardError) as ctx:
            load_board(self.repo)
        self.assertIn("99", str(ctx.exception))

    def test_cli_show_prints_both_blocks(self):
        task = self.new_task("Задача", description="описание")
        storage.add_gotcha(task, "Tk не thread-safe", flag="rake")
        storage.add_attempt(task, "пробовала libtk8.6", why="не взлетело")
        self.save()
        proc = self.run_cli("show", task["id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("грабли (1)", proc.stdout)
        self.assertIn("#rake Tk не thread-safe", proc.stdout)
        self.assertIn("неудачные пути (1)", proc.stdout)
        self.assertIn("пробовала libtk8.6", proc.stdout)


class TestFailCommand(TempRepoCase):
    """FB-904: команда fail — бортовой журнал неудачных путей.

    Знать, куда НЕ походили, часто ценнее знать финальный маршрут.
    """

    def run_cli(self, *args):
        env = self.cli_env()
        cmd = ["python3", str(REPO_ROOT / "board.py"), "--repo", str(self.repo), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))

    def test_fail_writes_attempt_and_keeps_column(self):
        task = self.new_task("Задача")
        move_task(self.data, task["id"], "wip")
        self.save()
        proc = self.run_cli("fail", task["id"], "Пробовала libtk8.6 с Xft",
                            "--why", "линкуется на libtcl8.6", "--tried", "apt-get download",
                            "--tag", "tk")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(fresh["column_id"], "wip", "fail не двигает карточку")
        self.assertEqual(len(fresh["attempts"]), 1)
        attempt = fresh["attempts"][0]
        self.assertEqual(attempt["text"], "Пробовала libtk8.6 с Xft")
        self.assertEqual(attempt["why"], "линкуется на libtcl8.6")
        self.assertEqual(attempt["tried"], "apt-get download")
        self.assertEqual(attempt["tags"], ["tk"])
        self.assertIn("failed", [e["kind"] for e in fresh["events"]],
                      "запись попадает в ленту событий")

    def test_fail_on_unknown_task_is_clear_error(self):
        proc = self.run_cli("fail", "K2C-999", "что-то")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("не найдена", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_fail_counter_visible_in_list(self):
        task = self.new_task("Задача")
        self.save()
        self.run_cli("fail", task["id"], "не взлетело")
        listing = self.run_cli("list").stdout
        self.assertIn("✗1", listing, "счётчик неудачных путей виден в списке")

    def test_fail_visible_in_show(self):
        task = self.new_task("Задача")
        self.save()
        self.run_cli("fail", task["id"], "не взлетело", "--why", "потому что")
        out = self.run_cli("show", task["id"]).stdout
        self.assertIn("неудачные пути (1)", out)
        self.assertIn("не взлетело", out)
        self.assertIn("потому что", out)


class TestFlagParsing(TempRepoCase):
    """FB-903: разбор флагов-граблей из сообщений коммитов."""

    def test_parse_flags_basic(self):
        self.assertEqual(gitlink.parse_flags("FB-12 #rake Тк не thread-safe"),
                         [{"flag": "rake", "text": "Тк не thread-safe"}])

    def test_parse_flags_several_in_one_commit(self):
        self.assertEqual(gitlink.parse_flags("FB-12 #rake A #gotcha B"),
                         [{"flag": "rake", "text": "A"}, {"flag": "gotcha", "text": "B"}])

    def test_parse_flags_ignores_unknown_hashtags(self):
        self.assertEqual(gitlink.parse_flags("FB-12 #неизвестный текст"), [])

    def test_parse_flags_stops_at_next_hashtag(self):
        self.assertEqual(gitlink.parse_flags("#rake грабля #FB-12 хвост"),
                         [{"flag": "rake", "text": "грабля"}])

    def test_parse_flags_keeps_text_after_flag_only(self):
        self.assertEqual(gitlink.parse_flags("feat: FB-12 #gotcha сокеты из /tmp подчищают"),
                         [{"flag": "gotcha", "text": "сокеты из /tmp подчищают"}])

    def test_parse_flags_ignores_flag_list(self):
        # перечисление флагов — не грабля: после флага нет разделителя
        self.assertEqual(gitlink.parse_flags("читает флаги #rake/#gotcha/#fix/#todo тут"), [])

    def test_parse_flags_keeps_one_line_only(self):
        found = gitlink.parse_flags("FB-12 #rake грабля\nвторая строка тела")
        self.assertEqual(found, [{"flag": "rake", "text": "грабля"}])

    def test_parse_flags_anchored_needs_flag_at_line_start(self):
        self.assertEqual(gitlink.parse_flags('пример: git commit -m "FB-12 #rake текст"',
                                             anchored=True), [])
        self.assertEqual(gitlink.parse_flags("#rake грабля отдельной строкой", anchored=True),
                         [{"flag": "rake", "text": "грабля отдельной строкой"}])

    def test_card_ids_in(self):
        self.assertEqual(gitlink.card_ids_in("FB-12 и FB-3", "FB"), ["FB-12", "FB-3"])
        self.assertEqual(gitlink.card_ids_in("FB-130", "FB"), ["FB-130"])
        self.assertEqual(gitlink.card_ids_in("ничего тут нет", "FB"), [])

    def test_flags_registry_extended_by_config(self):
        registry = config_mod.flags_registry({"flags": {"мой-флаг": "что это значит"}})
        self.assertIn("мой-флаг", registry)
        self.assertIn("rake", registry, "умолчания остаются")
        self.assertIn("gotcha", registry)

    def test_flags_registry_without_config_keeps_defaults(self):
        registry = config_mod.flags_registry({})
        self.assertEqual(sorted(registry), ["fix", "gotcha", "rake", "todo"])


class TestSyncCommand(TempRepoCase):
    """FB-903: коммит с флагом сам попадает в карточку, повторно — без дублей."""

    def run_cli(self, *args):
        env = self.cli_env()
        cmd = ["python3", str(REPO_ROOT / "board.py"), "--repo", str(self.repo), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))

    def commit(self, message: str) -> None:
        self._commits = getattr(self, "_commits", 0) + 1
        (self.repo / "file.txt").write_text(f"правка {self._commits}")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", message)

    def test_commit_flag_lands_in_card(self):
        task = self.new_task("Задача")
        self.save()
        self.commit(f"{task['id']} #rake Тк не thread-safe")
        proc = self.run_cli("sync")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(len(fresh["gotchas"]), 1)
        gotcha = fresh["gotchas"][0]
        self.assertEqual(gotcha["flag"], "rake")
        self.assertEqual(gotcha["text"], "Тк не thread-safe")
        self.assertEqual(gotcha["source"], "commit")
        self.assertTrue(gotcha["commit"], "коммит зафиксирован")

    def test_sync_twice_adds_no_duplicates(self):
        task = self.new_task("Задача")
        self.save()
        self.commit(f"{task['id']} #rake Тк не thread-safe")
        self.run_cli("sync")
        second = self.run_cli("sync")
        self.assertIn("граблей добавлено: 0", second.stdout)
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(len(fresh["gotchas"]), 1, "повторный разбор не плодит дубли")

    def test_flag_without_card_is_ignored(self):
        self.new_task("Задача")
        self.save()
        self.commit("#rake грабля без карточки")
        proc = self.run_cli("sync")
        self.assertIn("граблей добавлено: 0", proc.stdout)
        fresh = load_board(self.repo)["tasks"][0]
        self.assertEqual(fresh.get("gotchas"), None, "без карточки флаг не принимается")

    def test_other_card_is_not_touched(self):
        first = self.new_task("Первая")
        second = self.new_task("Вторая")
        self.save()
        self.commit(f"{second['id']} #rake грабля второй карточки")
        self.run_cli("sync", first["id"])
        board = load_board(self.repo)
        self.assertEqual(board["tasks"][0].get("gotchas"), None, "чужая карточка не цепляется")
        self.run_cli("sync")
        board = load_board(self.repo)
        by_id = {t["id"]: t for t in board["tasks"]}
        self.assertEqual(len(by_id[second["id"]]["gotchas"]), 1)
        self.assertEqual(by_id[first["id"]].get("gotchas"), None)

    def test_old_commits_without_flags_change_nothing(self):
        task = self.new_task("Задача")
        self.save()
        self.commit("обычный коммит без флагов")
        self.commit("fix: какая-то правка")
        proc = self.run_cli("sync")
        self.assertIn("граблей добавлено: 0", proc.stdout)
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(fresh.get("gotchas"), None)
        self.assertEqual(fresh["title"], "Задача", "карточка не пострадала")

    def test_example_in_body_does_not_pull_other_card(self):
        first = self.new_task("Первая")
        second = self.new_task("Вторая")
        self.save()
        self.commit(f"{first['id']} #rake грабля\n\n"
                    f'пример: git commit -m "{second["id"]} #rake чужая грабля"')
        self.run_cli("sync")
        board = load_board(self.repo)
        by_id = {t["id"]: t for t in board["tasks"]}
        self.assertEqual(len(by_id[first["id"]]["gotchas"]), 1)
        self.assertEqual(by_id[second["id"]].get("gotchas"), None,
                         "пример в теле не тянет чужую карточку")

    def test_gotcha_in_body_line_lands_in_card_from_subject(self):
        task = self.new_task("Задача")
        self.save()
        self.commit(f"feat: {task['id']}\n\n#rake грабля отдельной строкой")
        self.run_cli("sync")
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(len(fresh["gotchas"]), 1)
        self.assertEqual(fresh["gotchas"][0]["text"], "грабля отдельной строкой")

    def test_sync_unknown_card_is_clear_error(self):
        proc = self.run_cli("sync", "K2C-999")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("не найдена", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


class TestGitHook(TempRepoCase):
    """FB-906: после коммита грабли подхватываются сами, а не по памяти."""

    def run_cli(self, *args):
        env = self.cli_env()
        cmd = ["python3", str(REPO_ROOT / "board.py"), "--repo", str(self.repo), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))

    def commit(self, message: str):
        env = dict(os.environ, GIT_AUTHOR_NAME="Тест", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="Тест", GIT_COMMITTER_EMAIL="t@example.com")
        (self.repo / "file.txt").write_text(f"правка {message[:12]}")
        git(self.repo, "add", "-A")
        return subprocess.run(["git", "-C", str(self.repo), "commit", "-m", message],
                              capture_output=True, text=True, env=env)

    def test_hook_install_status_remove(self):
        proc = self.run_cli("hook", "install")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path = self.repo / ".git" / "hooks" / "post-commit"
        self.assertTrue(path.is_file())
        self.assertTrue(os.access(path, os.X_OK), "хук должен быть исполняемым")
        self.assertIn("FossilBoard", path.read_text(encoding="utf-8"))
        self.assertIn("установлен", self.run_cli("hook", "status").stdout)
        self.assertEqual(self.run_cli("hook", "remove").returncode, 0)
        self.assertFalse(path.exists())

    def test_commit_with_flag_lands_in_card_without_manual_sync(self):
        task = self.new_task("Задача")
        self.save()
        self.run_cli("hook", "install")
        proc = self.commit(f"{task['id']} #rake Тк не thread-safe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(len(fresh.get("gotchas") or []), 1, "хук сам дописал граблю")
        self.assertEqual(fresh["gotchas"][0]["flag"], "rake")
        self.assertEqual(fresh["gotchas"][0]["text"], "Тк не thread-safe")

    def test_hook_does_not_break_plain_commit(self):
        task = self.new_task("Задача")
        self.save()
        self.run_cli("hook", "install")
        proc = self.commit("обычный коммит без флагов")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(fresh.get("gotchas"), None, "обычный коммит граблей не создаёт")

    def test_hook_is_idempotent(self):
        task = self.new_task("Задача")
        self.save()
        self.run_cli("hook", "install")
        self.commit(f"{task['id']} #rake Тк не thread-safe")
        self.commit(f"{task['id']} ещё одна правка")
        fresh = find_task(load_board(self.repo), task["id"])
        self.assertEqual(len(fresh.get("gotchas") or []), 1, "повторный запуск не дублирует")

    def test_foreign_hook_is_not_overwritten(self):
        path = self.repo / ".git" / "hooks" / "post-commit"
        path.parent.mkdir(parents=True, exist_ok=True)
        original = "#!/bin/sh\necho мой собственный хук\n"
        path.write_text(original, encoding="utf-8")
        proc = self.run_cli("hook", "install")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("чужой хук", proc.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), original, "чужой хук не тронут")
        self.assertIn("не трогаю", self.run_cli("hook", "remove").stdout)

    def test_flags_for_task_reads_only_that_card(self):
        first = self.new_task("Первая")
        second = self.new_task("Вторая")
        self.save()
        self.commit(f"{first['id']} #rake грабля первой")
        self.commit(f"{second['id']} #gotcha грабля второй")
        found = gitlink.flags_for_task(str(self.repo), first["id"])
        self.assertEqual([f["text"] for f in found], ["грабля первой"])
        found_second = gitlink.flags_for_task(str(self.repo), second["id"])
        self.assertEqual([f["text"] for f in found_second], ["грабля второй"])


if __name__ == "__main__":
    unittest.main(verbosity=2)