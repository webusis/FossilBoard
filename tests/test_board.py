"""Тесты FossilBoard. Только stdlib unittest, без зависимостей.

    python3 -m unittest discover -s tests -v
"""

import json
import os
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

    def tearDown(self):
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
        (self.repo / ".board" / "board.json").write_text(
            (elsewhere / ".board" / "board.json").read_text(encoding="utf-8"), encoding="utf-8")
        proc = self.run_cli("init", "--id", "K2C", "--name", "Клон", "--repo", str(elsewhere))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("перерегистрирован", proc.stdout)
        board = load_board(elsewhere)
        self.assertEqual(len(board["tasks"]), 1, "карточки не должны пропасть")
        self.assertEqual(board["project"]["repo_path"], str(elsewhere.resolve()))
        listing = self.run_cli("list", repo=elsewhere)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn("Карточка из клона", listing.stdout)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)