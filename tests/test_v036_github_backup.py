"""GitHub private-repository backup: layout, sync, secrets, restore (all HTTP mocked)."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.storage.project_store import ProjectStore
from app.io.github_client import GitHubClient, GitHubError
from app.io.github_backup import GitHubBackupError, GitHubBackupService
from app.io.github_backup_layout import render_book, safe_name, strip_secrets
from app.core.config import APP_VERSION


TOKEN = "github_pat_" + "A" * 40


class FakeGitHub:
    """In-memory subset of the GitHub REST + Git Data API."""

    def __init__(self, *, login: str = "writer", repo_exists: bool = False, private: bool = True):
        self.login = login
        self.repos: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []
        self.created_repo_payloads: list[dict] = []
        self.fail_next: list[httpx.Response] = []
        self.request_bodies: list[bytes] = []
        if repo_exists:
            self._create_repo("inkforge-novels", private)

    # helpers
    def _sha(self, prefix: str, data: bytes) -> str:
        return hashlib.sha1(prefix.encode() + data).hexdigest()

    def _store_tree(self, files: dict[str, str]) -> str:
        sha = self._sha("tree", json.dumps(sorted(files.items())).encode())
        self.trees[sha] = dict(files)
        return sha

    def _create_repo(self, name: str, private: bool) -> dict:
        repo = {
            "name": name, "full_name": f"{self.login}/{name}", "private": private,
            "html_url": f"https://github.com/{self.login}/{name}", "default_branch": "main",
        }
        self.repos[name] = repo
        readme = b"# init\n"
        blob = self._sha("blob", readme)
        self.blobs[blob] = readme
        tree = self._store_tree({"README.md": blob})
        commit = self._sha("commit", tree.encode())
        self.commits[commit] = {"tree": tree, "parents": [], "message": "Initial commit"}
        self.refs[(name, "main")] = commit
        return repo

    def files(self, repo: str = "inkforge-novels", branch: str = "main") -> dict[str, bytes]:
        tree = self.trees[self.commits[self.refs[(repo, branch)]]["tree"]]
        return {path: self.blobs[sha] for path, sha in tree.items()}

    def head_message(self, repo: str = "inkforge-novels") -> str:
        return self.commits[self.refs[(repo, "main")]]["message"]

    # transport
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        self.calls.append((request.method, path))
        self.request_bodies.append(request.content)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if self.fail_next:
            return self.fail_next.pop(0)
        body = json.loads(request.content) if request.content else {}
        if path == "/user":
            return httpx.Response(200, json={"login": self.login})
        if path == "/user/repos" and request.method == "POST":
            self.created_repo_payloads.append(body)
            return httpx.Response(201, json=self._create_repo(body["name"], body["private"]))
        match = re.fullmatch(r"/repos/([^/]+)/([^/]+)(/.*)?", path)
        assert match, path
        _owner, name, rest = match.group(1), match.group(2), match.group(3) or ""
        if name not in self.repos:
            return httpx.Response(404, json={"message": "Not Found"})
        if rest == "":
            return httpx.Response(200, json=self.repos[name])
        if rest.startswith("/git/ref/heads/"):
            branch = rest[len("/git/ref/heads/"):]
            sha = self.refs.get((name, branch))
            if not sha:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": sha}})
        if rest.startswith("/git/refs/heads/") and request.method == "PATCH":
            branch = rest[len("/git/refs/heads/"):]
            parents = self.commits[body["sha"]]["parents"]
            if self.refs.get((name, branch)) not in parents:
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.refs[(name, branch)] = body["sha"]
            return httpx.Response(200, json={"object": {"sha": body["sha"]}})
        if rest.startswith("/git/commits/") and request.method == "GET":
            sha = rest.rsplit("/", 1)[1]
            return httpx.Response(200, json={"sha": sha, "tree": {"sha": self.commits[sha]["tree"]}})
        if rest == "/git/commits":
            sha = self._sha("commit", json.dumps(body).encode())
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return httpx.Response(201, json={"sha": sha})
        if rest == "/git/blobs":
            assert body["encoding"] == "base64"
            data = base64.b64decode(body["content"])
            sha = self._sha("blob", data)
            self.blobs[sha] = data
            return httpx.Response(201, json={"sha": sha})
        if rest.startswith("/git/blobs/"):
            sha = rest.rsplit("/", 1)[1]
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(self.blobs[sha]).decode()})
        if rest == "/git/trees":
            files = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry["sha"] is None:
                    if entry["path"] not in files:
                        return httpx.Response(422, json={"message": "path not in tree"})
                    files.pop(entry["path"])
                else:
                    files[entry["path"]] = entry["sha"]
            return httpx.Response(201, json={"sha": self._store_tree(files)})
        if rest.startswith("/git/trees/"):
            sha = rest.rsplit("/", 1)[1]
            items = [
                {"path": p, "type": "blob", "sha": s, "size": len(self.blobs[s])}
                for p, s in self.trees[sha].items()
            ]
            return httpx.Response(200, json={"sha": sha, "tree": items, "truncated": False})
        raise AssertionError(f"unexpected {request.method} {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def sample_project(store: ProjectStore, title: str = "雪夜：长歌/上*") -> dict:
    project = store.create(title)
    project["genre"] = "武侠"
    project["premise"] = "少年持剑入江湖。"
    project["outline"] = "第一卷入门，第二卷复仇。"
    project["book_rules"] = "不得复活死者。"
    project["settings"]["api_key"] = "sk-" + "s" * 30
    project["settings"]["research"]["brave_api_key"] = "brave-secret-value"
    project["settings"]["custom_access_token"] = "tok-should-vanish"
    project["characters"] = [
        {"name": "沈青", "importance": "main", "personality": "冷静"},
        {"name": "柳三", "importance": "supporting"},
        {"name": "旧人", "active": False},
    ]
    project["world_entries"] = [
        {"title": "剑阁", "category": "地点", "keys": ["剑阁", "阁"], "constant": True, "content": "山巅剑阁。"},
    ]
    project["planning"]["volumes"] = [
        {"title": "入门", "chapter_start": 1, "chapter_end": 1,
         "chapters": [{"title": "初见", "goal": "拜师"}]},
    ]
    project["chapters"] = [
        {"id": "c1", "title": "第一章 初见", "content": "沈青拔剑。\n\n风起。",
         "execution": {"audit_score": 86, "audit_verdict": "pass", "issues": [{"category": "节奏", "message": "略快"}]}},
        {"id": "c2", "title": "余波", "content": "柳三笑了。"},
    ]
    project["memory"]["plot_threads"] = [{"title": "断剑之谜", "status": "open", "setup": "断剑出现"}]
    project["memory"]["timeline"] = [{"time": "冬至", "event": "沈青下山"}]
    return store.save(project["id"], project)


@pytest.fixture()
def env(tmp_path: Path):
    store = ProjectStore(tmp_path / "gh.db", tmp_path / "gh-secrets.db")
    fake = FakeGitHub()
    service = GitHubBackupService(
        lambda: store, tmp_path / "gh-state.json", app_version="0.35.0",
        transport_factory=fake.transport, sleep=lambda _s: None,
    )
    service.update_settings({"token": TOKEN})
    return store, fake, service


def test_safe_names_are_windows_and_github_safe():
    assert safe_name('a/b\\c:d*e?"f<g>h|') == "a_b_c_d_e_f_g_h"
    assert safe_name("CON") == "_CON"
    assert safe_name("  ..  ") == "未命名"
    assert not safe_name(".hidden").startswith(".")
    assert len(safe_name("长" * 200)) == 60


def test_strip_secrets_is_recursive_and_keeps_numbers():
    clean = strip_secrets({
        "settings": {"api_key": "x", "max_tokens": 3500, "role_routes": {"a": {"api_key": "y"}},
                     "research": {"brave_api_key": "z"}, "github_token": "t"},
        "notes": ["ghp_" + "a" * 36, "普通文本"],
    })
    assert clean["settings"]["api_key"] == ""
    assert clean["settings"]["max_tokens"] == 3500
    assert clean["settings"]["role_routes"]["a"]["api_key"] == ""
    assert clean["settings"]["research"]["brave_api_key"] == ""
    assert clean["settings"]["github_token"] == ""
    assert clean["notes"] == ["", "普通文本"]


def test_render_book_layout(tmp_path: Path):
    store = ProjectStore(tmp_path / "r.db", tmp_path / "r-secrets.db")
    project = sample_project(store)
    files, volatile = render_book(project, include_binary_exports=True)
    paths = set(files)
    for expected in (
        "00-设定/核心构想.md", "00-设定/全书大纲.md", "00-设定/作者意图与硬规则.md", "00-设定/文风卡.md",
        "01-世界书/地点/剑阁.md", "02-人物卡/主要/沈青.md", "02-人物卡/配角/柳三.md", "02-人物卡/未启用/旧人.md",
        "03-规划/全书规划.md", "03-规划/卷01-入门.md",
        "04-正文/卷01-入门/第001章-初见.md", "04-正文/未分卷/第002章-余波.md",
        "05-记忆与线索/故事记忆.md", "05-记忆与线索/伏笔与线索.md", "05-记忆与线索/时间线.md", "05-记忆与线索/人物关系.md",
        "06-审校与精修/质量报告.md", ".inkforge/project.json",
    ):
        assert expected in paths, expected
    assert any(path.startswith("07-成品/") and path.endswith(".txt") for path in paths)
    assert {Path(path).suffix for path in volatile} == {".epub", ".docx"}
    world = files["01-世界书/地点/剑阁.md"].decode()
    assert world.startswith("---\n") and "constant: true" in world and '  - "剑阁"' in world
    chapter = files["04-正文/卷01-入门/第001章-初见.md"].decode()
    assert "# 第1章 初见" in chapter and "沈青拔剑" in chapter and "audit_score: 86" in chapter
    assert "略快" in files["06-审校与精修/质量报告.md"].decode()
    assert "断剑之谜" in files["05-记忆与线索/伏笔与线索.md"].decode()
    raw = files[".inkforge/project.json"].decode()
    assert "sk-sss" not in raw and "brave-secret-value" not in raw and "tok-should-vanish" not in raw
    assert json.loads(raw)["settings"]["max_tokens"] == project["settings"]["max_tokens"]


def test_connect_creates_private_repo_and_detects_owner(env):
    _store, fake, service = env
    result = service.connect()
    assert result["created"] is True and result["login"] == "writer"
    assert fake.created_repo_payloads == [{
        "name": "inkforge-novels", "private": True,
        "description": "砚火 InkForge 小说资料备份", "auto_init": True,
    }]
    settings = service.describe_settings()
    assert settings["owner"] == "writer" and settings["connected"] is True
    assert settings["token"] == "已设置" and TOKEN not in json.dumps(settings)


def test_connect_refuses_public_repo(tmp_path: Path):
    store = ProjectStore(tmp_path / "p.db", tmp_path / "p-secrets.db")
    fake = FakeGitHub(repo_exists=True, private=False)
    service = GitHubBackupService(lambda: store, tmp_path / "s.json", app_version="0.35.0",
                                  transport_factory=fake.transport, sleep=lambda _s: None)
    service.update_settings({"token": TOKEN})
    with pytest.raises(GitHubBackupError, match="公开仓库"):
        service.connect()
    sample_project(store)
    with pytest.raises(GitHubBackupError, match="公开"):
        service.sync_project(store.list()[0]["id"])
    assert not any(method == "POST" and "/git/" in path for method, path in fake.calls)


def test_sync_single_atomic_commit_skip_unchanged_and_delete(env, tmp_path: Path):
    store, fake, service = env
    service.connect()
    project = sample_project(store)
    result = service.sync_project(project["id"])
    assert not result["skipped"]
    folder = result["folder"]
    assert folder == "雪夜：长歌_上"
    files = fake.files()
    assert f"{folder}/04-正文/卷01-入门/第001章-初见.md" in files
    assert f"{folder}/README.md" in files and "README.md" in files and ".inkforge/books.json" in files
    assert "《雪夜：长歌/上*》" in fake.head_message() and "(砚火 v0.35.0)" in fake.head_message()
    assert "第1章" in fake.head_message()
    # one commit + one ref update per sync
    assert sum(1 for m, p in fake.calls if m == "POST" and p.endswith("/git/commits")) == 1
    assert sum(1 for m, p in fake.calls if m == "PATCH") == 1
    # no secret or token ever travels in a request body
    for body in fake.request_bodies:
        assert TOKEN.encode() not in body
    for data in files.values():
        assert b"sk-ssss" not in data and b"brave-secret-value" not in data

    # unchanged -> skipped, no new commit
    fake.calls.clear()
    assert service.sync_project(project["id"])["skipped"] is True
    assert not any(m == "POST" for m, _ in fake.calls)

    # edit one chapter, remove one character -> only changed blobs uploaded, removed file deleted
    current = store.get(project["id"])
    current["chapters"][1]["content"] = "柳三笑了，又哭了。"
    current["characters"] = current["characters"][:2]
    store.save(project["id"], current)
    fake.calls.clear()
    second = service.sync_project(project["id"])
    assert second["deleted"] == 1
    blob_posts = sum(1 for m, p in fake.calls if m == "POST" and p.endswith("/git/blobs"))
    assert blob_posts < 10
    files = fake.files()
    assert f"{folder}/02-人物卡/未启用/旧人.md" not in files
    assert "又哭了" in files[f"{folder}/04-正文/未分卷/第002章-余波.md"].decode()
    assert "第2章" in fake.head_message()

    state = json.loads((tmp_path / "gh-state.json").read_text(encoding="utf-8"))
    assert TOKEN not in json.dumps(state)
    assert state["books"][project["id"]]["slug"] == folder


def test_binary_exports_optional_and_removed_when_disabled(env):
    store, fake, service = env
    service.connect()
    project = sample_project(store, "铁骑")
    service.update_settings({"include_binary_exports": True})
    service.sync_project(project["id"])
    assert "铁骑/07-成品/铁骑.epub" in fake.files() and "铁骑/07-成品/铁骑.docx" in fake.files()
    assert fake.files()["铁骑/07-成品/铁骑.epub"][:2] == b"PK"
    # unchanged project: timestamped binaries do not force a new commit
    assert service.sync_project(project["id"])["skipped"] is True
    service.update_settings({"include_binary_exports": False})
    service.sync_project(project["id"])
    assert "铁骑/07-成品/铁骑.epub" not in fake.files()
    assert "铁骑/07-成品/铁骑.txt" in fake.files()


def test_retries_rate_limit_and_reports_chinese_errors(env):
    store, fake, service = env
    service.connect()
    project = sample_project(store)
    fake.fail_next = [httpx.Response(429, headers={"retry-after": "1"}, json={"message": "slow down"})]
    assert service.sync_project(project["id"])["skipped"] is False

    client = GitHubClient(TOKEN, transport=httpx.MockTransport(
        lambda request: httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"},
                                       json={"message": "API rate limit exceeded"})), sleep=lambda _s: None)
    with pytest.raises(GitHubError, match="频率超限"):
        client.user()
    client = GitHubClient(TOKEN, transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={"message": "Resource not accessible by personal access token"})),
        sleep=lambda _s: None)
    with pytest.raises(GitHubError, match="权限不足"):
        client.user()
    client = GitHubClient(TOKEN, transport=httpx.MockTransport(
        lambda request: httpx.Response(401, json={"message": "Bad credentials"})), sleep=lambda _s: None)
    with pytest.raises(GitHubError, match="令牌无效"):
        client.user()


def test_failed_sync_keeps_manifest_and_records_error(env):
    store, fake, service = env
    service.connect()
    project = sample_project(store)
    original = fake.handler

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/trees") and request.method == "POST":
            return httpx.Response(422, json={"message": "tree invalid"})
        return original(request)

    service.transport_factory = lambda: httpx.MockTransport(broken)
    with pytest.raises(GitHubBackupError, match="GitHub 拒绝"):
        service.sync_project(project["id"])
    status = service.status()
    book = next(item for item in status["books"] if item["project_id"] == project["id"])
    assert book["error"] and book["pending"] is True and not book["last_sync_at"]
    service.transport_factory = fake.transport
    assert service.sync_project(project["id"])["skipped"] is False


def test_auto_sync_is_debounced_per_project(env):
    store, fake, service = env
    service.connect()
    project = sample_project(store)
    now = [1_000_000.0]
    service.clock = lambda: now[0]
    assert service.run_auto_once() == []  # auto-sync off
    service.update_settings({"auto_sync": True})
    assert service.run_auto_once() == [project["id"]]
    current = store.get(project["id"])
    current["premise"] = "改了前提。"
    store.save(project["id"], current)
    now[0] += 60
    assert service.run_auto_once() == []  # within 10 minutes
    now[0] += 600
    assert service.run_auto_once() == [project["id"]]
    assert service.run_auto_once() == []  # nothing new


def test_restore_lists_remote_books_and_imports_as_new_project(env):
    store, fake, service = env
    service.connect()
    project = sample_project(store)
    service.sync_project(project["id"])
    books = service.remote_books()
    assert [book["title"] for book in books] == ["雪夜：长歌/上*"]
    before = {item["id"] for item in store.list()}
    restored = service.restore(books[0]["folder"])
    after = {item["id"] for item in store.list()}
    assert restored["id"] not in before and after == before | {restored["id"]}
    copy = store.get(restored["id"])
    assert copy["title"].endswith("（从 GitHub 恢复）")
    assert copy["chapters"][0]["content"] == "沈青拔剑。\n\n风起。"
    assert copy["settings"]["api_key"] == ""
    assert store.get(project["id"])["settings"]["api_key"].startswith("sk-")
    with pytest.raises(GitHubBackupError):
        service.restore("不存在")


def test_http_endpoints(monkeypatch, tmp_path: Path):
    import app.main as main

    fake = FakeGitHub()
    service = GitHubBackupService(lambda: main.store, tmp_path / "api-state.json", app_version="0.35.0",
                                  transport_factory=fake.transport, sleep=lambda _s: None)
    monkeypatch.setattr(main, "github_backup", service)
    client = TestClient(main.app)
    assert client.get("/api/github-backup/settings").json()["token_set"] is False
    assert client.post("/api/github-backup/sync-all").status_code == 400
    response = client.put("/api/github-backup/settings", json={"token": TOKEN, "repo": "my-novels", "auto_sync": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload["token"] == "已设置" and TOKEN not in response.text and payload["repo"] == "my-novels"
    assert client.put("/api/github-backup/settings", json={"repo": "坏 名字"}).status_code == 400
    connected = client.post("/api/github-backup/connect").json()
    assert connected["created"] and connected["owner"] == "writer"
    project = main.store.create("接口测试书")
    project["chapters"][0]["content"] = "正文一段。"
    main.store.save(project["id"], project)
    queued = client.post(f"/api/github-backup/sync/{project['id']}")
    assert queued.status_code == 200 and queued.json()["queued"]
    status = client.get("/api/github-backup/status").json()
    assert status["job"]["running"] is False and status["job"]["error"] == ""
    book = next(item for item in status["books"] if item["project_id"] == project["id"])
    assert book["last_sync_at"] and book["folder"] == "接口测试书"
    assert TOKEN not in json.dumps(status)
    assert client.post("/api/github-backup/sync/missing").status_code == 404
    remote = client.get("/api/github-backup/remote-books").json()["books"]
    assert any(item["folder"] == "接口测试书" for item in remote)
    restored = client.post("/api/github-backup/restore", json={"folder": "接口测试书"})
    assert restored.status_code == 200 and restored.json()["title"] == "接口测试书（从 GitHub 恢复）"
    main.store.delete(restored.json()["id"])
    main.store.delete(project["id"])
    cleared = client.put("/api/github-backup/settings", json={"clear_token": True}).json()
    assert cleared["token_set"] is False


def test_frontend_entry_registered():
    root = Path(__file__).resolve().parents[1] / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    assert 'id="githubBackupBtn"' in html
    assert f"/assets/js/features/github-backup.js?v={APP_VERSION}" in html
    source = (root / "js" / "features" / "github-backup.js").read_text(encoding="utf-8")
    assert 'Inkforge.register("githubBackup"' in source
    for phrase in ("Contents", "Metadata", "Administration", "连接并创建私人仓库", "从 GitHub 恢复"):
        assert phrase in source
