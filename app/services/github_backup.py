"""Back up every novel into one private GitHub repository of the author's.

* Settings and per-book manifests live in a small JSON state file next to the
  database; the token lives only in the encrypted secret store.
* One sync = one atomic commit built with the Git Data API.  Unchanged files
  are skipped via content hashes; files removed since the last sync are
  deleted with ``sha: null`` tree entries.
* Restore always imports as a NEW project.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Protocol

from ..infrastructure.github_client import GitHubClient, GitHubError
from .github_backup_layout import (
    chapter_words,
    render_book,
    book_readme,
    safe_name,
    unique_name,
    written_chapter_count,
)


logger = logging.getLogger(__name__)

SECRET_SCOPE = "__app__:github_backup"
SECRET_NAME = "token"
REPO_DESCRIPTION = "砚火 InkForge 小说资料备份"
DEFAULT_REPO = "inkforge-novels"
DEFAULT_BRANCH = "main"
AUTO_SYNC_INTERVAL = 600.0
MAPPING_PATH = ".inkforge/books.json"
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_BRANCH_NAME = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,200}(?<!/)(?<!\.lock)$")


class SecretBackend(Protocol):
    def get(self, project_id: str) -> dict[str, str]: ...
    def replace(self, project_id: str, values: dict[str, str]) -> None: ...


class ProjectBackend(Protocol):
    secret_store: SecretBackend

    def list(self) -> list[dict[str, Any]]: ...
    def get(self, project_id: str) -> dict[str, Any] | None: ...
    def import_project(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class GitHubBackupError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def default_settings() -> dict[str, Any]:
    return {
        "owner": "",
        "repo": DEFAULT_REPO,
        "branch": DEFAULT_BRANCH,
        "auto_sync": False,
        "include_binary_exports": False,
    }


class GitHubBackupService:
    def __init__(
        self,
        store_provider: Callable[[], ProjectBackend],
        state_path: Path,
        *,
        app_version: str,
        transport_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store_provider = store_provider
        self.state_path = Path(state_path)
        self.app_version = app_version
        self.transport_factory = transport_factory
        self.clock = clock
        self.sleep = sleep
        self._state_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._job: dict[str, Any] = {"running": False, "kind": "", "started_at": "", "finished_at": "", "message": "", "error": ""}
        self._state = self._load()

    # ------------------------------------------------------------- state

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        settings = default_settings()
        if isinstance(payload.get("settings"), dict):
            settings.update({key: payload["settings"][key] for key in settings if key in payload["settings"]})
        books = payload.get("books") if isinstance(payload.get("books"), dict) else {}
        repo = payload.get("repo") if isinstance(payload.get("repo"), dict) else {}
        root = payload.get("root") if isinstance(payload.get("root"), dict) else {}
        return {"settings": settings, "books": books, "repo": repo, "root": root, "last_error": str(payload.get("last_error", ""))}

    def _save(self) -> None:
        with self._state_lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.state_path)

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self._state["settings"])

    def _token(self) -> str:
        try:
            return str(self.store_provider().secret_store.get(SECRET_SCOPE).get(SECRET_NAME, ""))
        except Exception:  # noqa: BLE001 - a broken secret store must not crash status
            logger.exception("GitHub token could not be read")
            return ""

    def _client(self) -> GitHubClient:
        token = self._token()
        if not token:
            raise GitHubBackupError("尚未设置 GitHub 令牌")
        transport = self.transport_factory() if self.transport_factory else None
        return GitHubClient(
            token, transport=transport, sleep=self.sleep,
            user_agent=f"InkForge/{self.app_version}",
        )

    def describe_settings(self) -> dict[str, Any]:
        settings = self.settings
        repo = dict(self._state.get("repo") or {})
        return {
            **settings,
            "token": "已设置" if self._token() else "",
            "token_set": bool(self._token()),
            "connected": bool(repo.get("full_name")) and bool(repo.get("private")),
            "repo_url": repo.get("html_url", ""),
            "repo_private": bool(repo.get("private")),
            "repo_full_name": repo.get("full_name", ""),
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            settings = self._state["settings"]
            previous_target = (settings.get("owner"), settings.get("repo"), settings.get("branch"))
            if "repo" in payload:
                repo = str(payload.get("repo") or "").strip() or DEFAULT_REPO
                if not _REPO_NAME.match(repo) or repo in {".", ".."}:
                    raise GitHubBackupError("仓库名只能包含英文字母、数字、点、短横线和下划线")
                settings["repo"] = repo
            if "branch" in payload:
                branch = str(payload.get("branch") or "").strip() or DEFAULT_BRANCH
                if not _BRANCH_NAME.match(branch):
                    raise GitHubBackupError("分支名无效")
                settings["branch"] = branch
            if "owner" in payload and str(payload.get("owner") or "").strip():
                owner = str(payload["owner"]).strip()
                if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
                    raise GitHubBackupError("GitHub 用户名无效")
                settings["owner"] = owner
            for key in ("auto_sync", "include_binary_exports"):
                if key in payload:
                    settings[key] = bool(payload[key])
            token = payload.get("token")
            if isinstance(token, str) and token.strip() and token.strip() != "已设置":
                self.store_provider().secret_store.replace(SECRET_SCOPE, {SECRET_NAME: token.strip()})
                self._state["repo"] = {}
            elif payload.get("clear_token"):
                self.store_provider().secret_store.replace(SECRET_SCOPE, {})
                self._state["repo"] = {}
            current_target = (settings.get("owner"), settings.get("repo"), settings.get("branch"))
            if current_target != previous_target:
                # A different repository/branch: manifests no longer describe it.
                self._state["repo"] = {}
                self._state["books"] = {
                    key: {"slug": value.get("slug", ""), "title": value.get("title", "")}
                    for key, value in self._state["books"].items()
                    if isinstance(value, dict)
                }
                self._state["root"] = {}
            self._save()
        return self.describe_settings()

    # ------------------------------------------------------------- connect

    def connect(self) -> dict[str, Any]:
        settings = self.settings
        created = False
        with self._client() as client:
            user = client.user()
            owner = str(user.get("login", "")).strip()
            if not owner:
                raise GitHubBackupError("无法从令牌识别 GitHub 用户名")
            repo = client.get_repo(owner, settings["repo"])
            if repo is None:
                repo = client.create_private_repo(settings["repo"], REPO_DESCRIPTION)
                created = True
        with self._state_lock:
            self._state["settings"]["owner"] = owner
            self._state["repo"] = {
                "full_name": repo.get("full_name", f"{owner}/{settings['repo']}"),
                "html_url": repo.get("html_url", f"https://github.com/{owner}/{settings['repo']}"),
                "private": bool(repo.get("private")),
                "default_branch": repo.get("default_branch", DEFAULT_BRANCH),
                "checked_at": utc_now(),
            }
            self._state["last_error"] = ""
            self._save()
        if not repo.get("private"):
            raise GitHubBackupError(
                f"仓库 {repo.get('full_name', settings['repo'])} 是公开仓库，为保护书稿已拒绝同步。"
                "请在 GitHub 把它改为 Private，或换一个仓库名"
            )
        return {**self.describe_settings(), "created": created, "login": owner}

    def _verified_repo(self, client: GitHubClient) -> tuple[str, str, str]:
        settings = self.settings
        owner = settings.get("owner") or str(client.user().get("login", ""))
        if not owner:
            raise GitHubBackupError("请先连接 GitHub")
        repo = client.get_repo(owner, settings["repo"])
        if repo is None:
            raise GitHubBackupError("备份仓库不存在，请先点击“连接并创建私人仓库”")
        if not repo.get("private"):
            with self._state_lock:
                self._state["repo"] = {**self._state.get("repo", {}), "private": False}
                self._save()
            raise GitHubBackupError("备份仓库是公开仓库，为保护书稿已拒绝同步")
        with self._state_lock:
            self._state["settings"]["owner"] = owner
            self._state["repo"] = {
                "full_name": repo.get("full_name", f"{owner}/{settings['repo']}"),
                "html_url": repo.get("html_url", ""),
                "private": True,
                "default_branch": repo.get("default_branch", DEFAULT_BRANCH),
                "checked_at": utc_now(),
            }
        return owner, settings["repo"], settings["branch"]

    def _head(self, client: GitHubClient, owner: str, repo: str, branch: str) -> str:
        sha = client.get_branch_sha(owner, repo, branch)
        if sha:
            return sha
        default_branch = str(self._state.get("repo", {}).get("default_branch") or DEFAULT_BRANCH)
        base = client.get_branch_sha(owner, repo, default_branch) if default_branch != branch else None
        if base:
            client.create_ref(owner, repo, branch, base)
            return base
        # Empty repository: the Git Data API needs one commit to exist first.
        client.put_contents(
            owner, repo, "README.md", "# 砚火小说备份\n".encode("utf-8"),
            "初始化砚火备份仓库", branch if default_branch != branch else None,
        )
        sha = client.get_branch_sha(owner, repo, branch)
        if not sha:
            raise GitHubBackupError(f"无法创建分支 {branch}")
        return sha

    # ------------------------------------------------------------- sync

    def _book_slug(self, project: dict[str, Any]) -> str:
        project_id = str(project.get("id", ""))
        books = self._state["books"]
        existing = books.get(project_id, {}) if isinstance(books.get(project_id), dict) else {}
        if existing.get("slug"):
            return str(existing["slug"])
        used = {
            str(value.get("slug", "")).casefold()
            for key, value in books.items()
            if key != project_id and isinstance(value, dict)
        }
        used |= {".inkforge", "readme.md"}
        return unique_name(safe_name(project.get("title"), "未命名作品"), used)

    def _root_readme(self, books_state: dict[str, Any]) -> bytes:
        lines = [
            "# 砚火小说备份",
            "",
            f"由砚火 InkForge v{self.app_version} 自动维护。每本书一个文件夹；`.inkforge/project.json` 可在砚火中“从 GitHub 恢复”。",
            "",
            "| 书名 | 文件夹 | 章节 | 字数 | 最近同步 |",
            "|---|---|---|---|---|",
        ]
        books = sorted(
            (value for value in books_state.values() if isinstance(value, dict) and value.get("last_sync_at")),
            key=lambda item: str(item.get("title", "")),
        )
        for book in books:
            slug = str(book.get("slug", ""))
            link = slug.replace(" ", "%20")
            lines.append(
                f"| {str(book.get('title', '')).replace('|', '/')} | [{slug}](./{link}/) | "
                f"{book.get('chapters', 0)} | {int(book.get('words', 0) or 0):,} | {book.get('last_sync_at', '')} |"
            )
        if not books:
            lines.append("| （暂无） | | | | |")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _mapping(self, books_state: dict[str, Any]) -> bytes:
        mapping = {
            "app": "InkForge",
            "version": self.app_version,
            "books": {
                key: {"folder": value.get("slug", ""), "title": value.get("title", ""), "last_sync_at": value.get("last_sync_at", "")}
                for key, value in sorted(books_state.items())
                if isinstance(value, dict) and value.get("last_sync_at")
            },
        }
        return (json.dumps(mapping, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def _commit_message(self, project: dict[str, Any], changed: list[str], deleted: list[str]) -> str:
        title = str(project.get("title", "")).strip() or "未命名作品"
        numbers = []
        for path in changed:
            match = re.search(r"04-正文/[^/]+/第(\d+)章", path)
            if match:
                numbers.append(int(match.group(1)))
        numbers.sort()
        if numbers:
            listed = "、".join(f"第{number}章" for number in numbers[:3])
            summary = f"{listed}…等{len(numbers)}章" if len(numbers) > 3 else listed
        else:
            written = written_chapter_count(project)
            summary = f"更新资料（已写{written}章）"
        if deleted and not numbers:
            summary += f"，移除{len(deleted)}个文件"
        return f"备份《{title}》：{summary} (砚火 v{self.app_version})"

    def sync_project(self, project_id: str, *, force: bool = False) -> dict[str, Any]:
        with self._sync_lock:
            try:
                result = self._sync_project_locked(project_id, force=force)
            except (GitHubError, GitHubBackupError) as exc:
                self._record_error(project_id, str(exc))
                raise GitHubBackupError(str(exc)) from exc
            return result

    def _record_error(self, project_id: str, message: str) -> None:
        with self._state_lock:
            book = self._state["books"].setdefault(project_id, {})
            if isinstance(book, dict):
                book["error"] = message
                book["last_attempt_at"] = utc_now()
                book["last_attempt_ts"] = self.clock()
            self._state["last_error"] = message
            self._save()

    def _sync_project_locked(self, project_id: str, *, force: bool) -> dict[str, Any]:
        project = self.store_provider().get(project_id)
        if project is None:
            raise GitHubBackupError("作品不存在")
        project = deepcopy(project)
        settings = self.settings
        slug = self._book_slug(project)
        book_state = self._state["books"].get(project_id) if isinstance(self._state["books"].get(project_id), dict) else {}
        previous: dict[str, str] = dict(book_state.get("files") or {}) if not force else {}
        files, volatile = render_book(project, include_binary_exports=bool(settings["include_binary_exports"]))
        txt_fingerprint = next((_sha(data) for path, data in files.items() if path.startswith("07-成品/")), "")
        fingerprints = {path: _sha(data) for path, data in files.items()}
        for path in volatile:
            fingerprints[path] = _sha(f"{path}:{txt_fingerprint}".encode("utf-8"))
        changed = [path for path, digest in fingerprints.items() if previous.get(path) != digest]
        deleted = sorted(set(previous) - set(fingerprints) - {"README.md"})
        if not changed and not deleted and book_state.get("slug") == slug and not force:
            with self._state_lock:
                book_state.update({"error": "", "last_attempt_at": utc_now(), "last_attempt_ts": self.clock(),
                                   "synced_updated_at": str(project.get("updated_at", ""))})
                self._state["books"][project_id] = book_state
                self._save()
            return {"project_id": project_id, "skipped": True, "message": "内容无变化，已跳过", "changed": 0, "deleted": 0}

        now = utc_now()
        uploads: dict[str, bytes] = {path: (files.get(path) if path in files else volatile[path]) for path in changed}
        uploads["README.md"] = book_readme(project, now)
        fingerprints["README.md"] = _sha(uploads["README.md"])

        with self._client() as client:
            owner, repo, branch = self._verified_repo(client)
            for attempt in range(2):
                head = self._head(client, owner, repo, branch)
                base_tree = str(client.get_commit(owner, repo, head)["tree"]["sha"])
                prefix = f"{slug}/"
                deletions = [prefix + path for path in deleted]
                if deletions:
                    tree = client.get_tree(owner, repo, base_tree, recursive=True)
                    if not tree.get("truncated"):
                        present = {item.get("path") for item in tree.get("tree", []) if item.get("type") == "blob"}
                        deletions = [path for path in deletions if path in present]
                entries = []
                for path, data in sorted(uploads.items()):
                    entries.append({"path": prefix + path, "mode": "100644", "type": "blob", "sha": client.create_blob(owner, repo, data)})
                entries.extend({"path": path, "mode": "100644", "type": "blob", "sha": None} for path in deletions)
                # Root index + mapping, rendered as if this sync already succeeded.
                pending = deepcopy(self._state["books"])
                pending[project_id] = {
                    **(book_state or {}), "slug": slug, "title": project.get("title", ""), "last_sync_at": now,
                    "chapters": written_chapter_count(project), "words": chapter_words(project),
                }
                root_files = {"README.md": self._root_readme(pending), MAPPING_PATH: self._mapping(pending)}
                root_hashes = {path: _sha(data) for path, data in root_files.items()}
                for path, data in root_files.items():
                    if self._state["root"].get(path) != root_hashes[path] or force:
                        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": client.create_blob(owner, repo, data)})
                tree_sha = client.create_tree(owner, repo, base_tree, entries)
                message = self._commit_message(project, changed, deleted)
                commit_sha = client.create_commit(owner, repo, message, tree_sha, [head])
                try:
                    client.update_ref(owner, repo, branch, commit_sha)
                    break
                except GitHubError as exc:
                    if exc.status == 422 and attempt == 0:
                        continue  # branch moved underneath us; rebuild on the new head
                    raise

        with self._state_lock:
            self._state["books"][project_id] = {
                "slug": slug,
                "title": project.get("title", ""),
                "files": fingerprints,
                "last_sync_at": now,
                "last_attempt_at": now,
                "last_attempt_ts": self.clock(),
                "last_commit": commit_sha,
                "synced_updated_at": str(project.get("updated_at", "")),
                "chapters": written_chapter_count(project),
                "words": chapter_words(project),
                "error": "",
            }
            self._state["root"] = root_hashes
            self._state["last_error"] = ""
            self._save()
        return {
            "project_id": project_id, "skipped": False, "commit": commit_sha, "message": message,
            "changed": len(changed), "deleted": len(deleted), "folder": slug,
        }

    def sync_all(self) -> dict[str, Any]:
        results = []
        errors = []
        for item in self.store_provider().list():
            try:
                results.append(self.sync_project(str(item["id"])))
            except GitHubBackupError as exc:
                errors.append({"project_id": item["id"], "title": item.get("title", ""), "error": str(exc)})
        return {"results": results, "errors": errors}

    # ------------------------------------------------------------- background jobs

    def job_status(self) -> dict[str, Any]:
        return dict(self._job)

    def begin_job(self, kind: str) -> bool:
        with self._state_lock:
            if self._job.get("running"):
                return False
            self._job = {"running": True, "kind": kind, "started_at": utc_now(), "finished_at": "", "message": "正在备份……", "error": ""}
            return True

    def run_job(self, kind: str, project_id: str = "") -> None:
        """Body of a background job; never raises."""
        try:
            if kind == "project":
                result = self.sync_project(project_id)
                message = result.get("message", "")
            else:
                result = self.sync_all()
                message = f"已处理 {len(result['results'])} 本，失败 {len(result['errors'])} 本"
                if result["errors"]:
                    raise GitHubBackupError("；".join(f"《{item['title']}》：{item['error']}" for item in result["errors"][:3]))
            self._job.update({"running": False, "finished_at": utc_now(), "message": message, "error": ""})
        except Exception as exc:  # noqa: BLE001 - surfaced through the status endpoint
            if not isinstance(exc, GitHubBackupError):
                logger.exception("GitHub backup job failed")
            self._job.update({"running": False, "finished_at": utc_now(), "message": "", "error": str(exc) or type(exc).__name__})

    def status(self) -> dict[str, Any]:
        projects = {str(item["id"]): item for item in self.store_provider().list()}
        books = []
        for project_id, project in projects.items():
            state = self._state["books"].get(project_id) if isinstance(self._state["books"].get(project_id), dict) else {}
            books.append({
                "project_id": project_id,
                "title": project.get("title", ""),
                "folder": state.get("slug", ""),
                "last_sync_at": state.get("last_sync_at", ""),
                "last_commit": state.get("last_commit", ""),
                "error": state.get("error", ""),
                "pending": str(project.get("updated_at", "")) != str(state.get("synced_updated_at", "")),
            })
        return {
            "settings": self.describe_settings(),
            "job": self.job_status(),
            "books": books,
            "last_error": self._state.get("last_error", ""),
        }

    def due_projects(self) -> list[str]:
        settings = self.settings
        if not settings.get("auto_sync") or not self._token() or not self._state.get("repo", {}).get("private"):
            return []
        now = self.clock()
        due = []
        for item in self.store_provider().list():
            project_id = str(item["id"])
            state = self._state["books"].get(project_id) if isinstance(self._state["books"].get(project_id), dict) else {}
            if str(item.get("updated_at", "")) == str(state.get("synced_updated_at", "")):
                continue
            if now - float(state.get("last_attempt_ts", 0) or 0) < AUTO_SYNC_INTERVAL:
                continue
            due.append(project_id)
        return due

    def run_auto_once(self) -> list[str]:
        synced = []
        for project_id in self.due_projects():
            try:
                self.sync_project(project_id)
                synced.append(project_id)
            except GitHubBackupError as exc:
                logger.warning("GitHub auto backup failed for %s: %s", project_id, exc)
        return synced

    async def run(self, stop: Any, interval: float = 60.0) -> None:
        import asyncio

        while not stop.is_set():
            try:
                await asyncio.to_thread(self.run_auto_once)
            except Exception:  # noqa: BLE001
                logger.exception("GitHub auto backup loop failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------- restore

    def _remote_index(self, client: GitHubClient) -> tuple[str, str, list[dict[str, Any]]]:
        owner, repo, branch = self._verified_repo(client)
        head = client.get_branch_sha(owner, repo, branch)
        if not head:
            return owner, repo, []
        tree_sha = str(client.get_commit(owner, repo, head)["tree"]["sha"])
        tree = client.get_tree(owner, repo, tree_sha, recursive=True)
        entries = {item.get("path"): item for item in tree.get("tree", []) if item.get("type") == "blob"}
        titles: dict[str, dict[str, Any]] = {}
        mapping_entry = entries.get(MAPPING_PATH)
        if mapping_entry:
            try:
                mapping = json.loads(client.get_blob(owner, repo, str(mapping_entry["sha"])).decode("utf-8"))
                for project_id, info in (mapping.get("books") or {}).items():
                    if isinstance(info, dict) and info.get("folder"):
                        titles[str(info["folder"])] = {**info, "project_id": project_id}
            except (ValueError, UnicodeDecodeError, AttributeError):
                pass
        books = []
        for path, item in sorted(entries.items()):
            if not path.endswith("/.inkforge/project.json") or path.count("/") != 2:
                continue
            folder = path.split("/", 1)[0]
            info = titles.get(folder, {})
            books.append({
                "folder": folder,
                "title": info.get("title") or folder,
                "last_sync_at": info.get("last_sync_at", ""),
                "source_project_id": info.get("project_id", ""),
                "sha": item.get("sha", ""),
                "size": item.get("size", 0),
            })
        return owner, repo, books

    def remote_books(self) -> list[dict[str, Any]]:
        with self._client() as client:
            _owner, _repo, books = self._remote_index(client)
        return [{key: value for key, value in book.items() if key != "sha"} for book in books]

    def restore(self, folder: str) -> dict[str, Any]:
        with self._client() as client:
            owner, repo, books = self._remote_index(client)
            book = next((item for item in books if item["folder"] == folder), None)
            if book is None:
                raise GitHubBackupError("仓库中没有找到这本书的备份")
            raw = client.get_blob(owner, repo, str(book["sha"]))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubBackupError("备份中的 project.json 已损坏，无法恢复") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("chapters"), list):
            raise GitHubBackupError("备份中的 project.json 不是有效的砚火作品")
        title = str(payload.get("title", "")).strip() or folder
        payload["title"] = f"{title}（从 GitHub 恢复）"
        created = self.store_provider().import_project(payload)
        return {"project": created, "title": created.get("title", ""), "id": created.get("id", "")}
