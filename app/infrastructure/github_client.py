"""Minimal GitHub REST client used by the private-repository backup.

Only the handful of endpoints the backup needs are wrapped.  Every failure is
translated into :class:`GitHubError` with a Chinese, author-facing message; the
token itself never appears in messages or logs.  Tests inject an
``httpx.MockTransport`` so no real network traffic happens.
"""
from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx


API_ROOT = "https://api.github.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _quote_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = "InkForge-Backup",
    ):
        if not str(token or "").strip():
            raise GitHubError("尚未填写 GitHub 令牌")
        self._client = httpx.Client(
            base_url=API_ROOT,
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token.strip()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": user_agent,
            },
        )
        self.max_retries = max(0, int(max_retries))
        self.sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ core

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after", "")
            if retry_after.isdigit():
                return min(60.0, float(retry_after))
            reset = response.headers.get("x-ratelimit-reset", "")
            if response.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
                return max(1.0, min(60.0, float(reset) - time.time()))
        return min(30.0, 1.0 * (2 ** attempt))

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        try:
            message = str(response.json().get("message", "")).lower()
        except ValueError:
            message = ""
        return "rate limit" in message

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        message = str(payload.get("message", ""))
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            detail = "; ".join(
                str(item.get("message") or item.get("code") or item)
                if isinstance(item, dict)
                else str(item)
                for item in errors[:3]
            )
            message = f"{message}（{detail}）" if message else detail
        return message[:300]

    def _error(self, response: httpx.Response, action: str) -> GitHubError:
        status = response.status_code
        detail = self._message(response)
        if status == 401:
            text = "GitHub 令牌无效或已过期，请重新生成并保存"
        elif self._is_rate_limited(response):
            text = "GitHub 访问频率超限，请稍后再试（已自动重试仍失败）"
        elif status == 403:
            text = (
                f"GitHub 拒绝了{action}：令牌权限不足。请确认令牌对该仓库有 "
                "Contents 读写、Metadata 只读权限；创建仓库还需要 Administration 读写"
            )
        elif status == 404:
            text = f"{action}失败：仓库或分支不存在，或令牌无权访问该仓库"
        elif status == 409:
            text = f"{action}失败：仓库为空或存在冲突"
        elif status == 422:
            text = f"{action}失败：GitHub 拒绝了提交内容"
        elif status >= 500:
            text = f"GitHub 服务暂时不可用（{status}），请稍后重试"
        else:
            text = f"{action}失败（HTTP {status}）"
        if detail and status not in {401}:
            text += f"：{detail}"
        return GitHubError(text, status)

    def request(
        self,
        method: str,
        url: str,
        *,
        action: str = "GitHub 请求",
        allow: tuple[int, ...] = (),
        **kwargs: Any,
    ) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    self.sleep(self._retry_delay(None, attempt))
                    continue
                raise GitHubError(f"{action}超时，请检查网络后重试") from exc
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    self.sleep(self._retry_delay(None, attempt))
                    continue
                raise GitHubError(f"无法连接 GitHub（{action}）：{type(exc).__name__}") from exc
            last = response
            if response.status_code < 400 or response.status_code in allow:
                return response
            retryable = response.status_code in RETRY_STATUSES or self._is_rate_limited(response)
            if retryable and attempt < self.max_retries:
                self.sleep(self._retry_delay(response, attempt))
                continue
            raise self._error(response, action)
        assert last is not None  # pragma: no cover - loop always returns/raises
        raise self._error(last, action)

    def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self.request(method, url, **kwargs)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    # ----------------------------------------------------------- endpoints

    def user(self) -> dict[str, Any]:
        return self.json("GET", "/user", action="验证令牌")

    def get_repo(self, owner: str, repo: str) -> dict[str, Any] | None:
        response = self.request(
            "GET", f"/repos/{quote(owner)}/{quote(repo)}", action="读取仓库", allow=(404,)
        )
        return None if response.status_code == 404 else response.json()

    def create_private_repo(self, name: str, description: str) -> dict[str, Any]:
        return self.json(
            "POST",
            "/user/repos",
            action="创建私人仓库",
            json={
                "name": name,
                "private": True,
                "description": description,
                "auto_init": True,
            },
        )

    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str | None:
        response = self.request(
            "GET",
            f"/repos/{quote(owner)}/{quote(repo)}/git/ref/heads/{_quote_path(branch)}",
            action="读取分支",
            allow=(404, 409),
        )
        if response.status_code in {404, 409}:
            return None
        return str(response.json().get("object", {}).get("sha", "")) or None

    def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> None:
        self.json(
            "POST",
            f"/repos/{quote(owner)}/{quote(repo)}/git/refs",
            action="创建分支",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def put_contents(
        self, owner: str, repo: str, path: str, content: bytes, message: str, branch: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
        }
        if branch:
            body["branch"] = branch
        return self.json(
            "PUT",
            f"/repos/{quote(owner)}/{quote(repo)}/contents/{_quote_path(path)}",
            action="初始化仓库",
            json=body,
        )

    def get_commit(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        return self.json(
            "GET", f"/repos/{quote(owner)}/{quote(repo)}/git/commits/{sha}", action="读取提交"
        )

    def get_tree(self, owner: str, repo: str, sha: str, recursive: bool = True) -> dict[str, Any]:
        return self.json(
            "GET",
            f"/repos/{quote(owner)}/{quote(repo)}/git/trees/{sha}",
            action="读取文件树",
            params={"recursive": "1"} if recursive else None,
        )

    def create_blob(self, owner: str, repo: str, content: bytes) -> str:
        payload = self.json(
            "POST",
            f"/repos/{quote(owner)}/{quote(repo)}/git/blobs",
            action="上传文件",
            json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        return str(payload["sha"])

    def get_blob(self, owner: str, repo: str, sha: str) -> bytes:
        payload = self.json(
            "GET", f"/repos/{quote(owner)}/{quote(repo)}/git/blobs/{sha}", action="下载文件"
        )
        if payload.get("encoding") == "base64":
            return base64.b64decode(str(payload.get("content", "")))
        return str(payload.get("content", "")).encode("utf-8")

    def create_tree(self, owner: str, repo: str, base_tree: str, entries: list[dict[str, Any]]) -> str:
        payload = self.json(
            "POST",
            f"/repos/{quote(owner)}/{quote(repo)}/git/trees",
            action="生成文件树",
            json={"base_tree": base_tree, "tree": entries},
        )
        return str(payload["sha"])

    def create_commit(self, owner: str, repo: str, message: str, tree: str, parents: list[str]) -> str:
        payload = self.json(
            "POST",
            f"/repos/{quote(owner)}/{quote(repo)}/git/commits",
            action="创建提交",
            json={"message": message, "tree": tree, "parents": parents},
        )
        return str(payload["sha"])

    def update_ref(self, owner: str, repo: str, branch: str, sha: str) -> None:
        self.json(
            "PATCH",
            f"/repos/{quote(owner)}/{quote(repo)}/git/refs/heads/{_quote_path(branch)}",
            action="更新分支",
            json={"sha": sha, "force": False},
        )
