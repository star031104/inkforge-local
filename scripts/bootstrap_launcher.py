"""Self-contained Windows bootstrapper used to build InkForgeLauncher.exe.

The module intentionally uses only the Python standard library. The compiled
launcher can therefore prepare a managed Python runtime and application virtual
environment before any InkForge dependency is importable.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.error import URLError
from urllib.request import Request, urlopen
import webbrowser


APP_VERSION = "0.33.2"
PYTHON_VERSION = "3.12.10"
PYTHON_INSTALLER_URL = (
    "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
)
PYTHON_INSTALLER_SHA256 = (
    "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
)
MINIMUM_PYTHON = (3, 10)


class LauncherError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LaunchOptions:
    source_root: Path
    runtime_root: Path
    host: str = "127.0.0.1"
    port: int = 7860
    open_browser: bool = True
    dry_run: bool = False
    smoke: bool = False


@dataclass(frozen=True, slots=True)
class LaunchResult:
    url: str
    reused_server: bool
    process_id: int | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def requirements_fingerprint(source_root: Path) -> str:
    return sha256_file(source_root / "requirements.txt")


def is_source_root(path: Path) -> bool:
    return all(
        candidate.is_file()
        for candidate in (
            path / "app" / "main.py",
            path / "static" / "index.html",
            path / "requirements.txt",
        )
    )


def discover_source_root(explicit: str = "") -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_root = os.environ.get("INKFORGE_LAUNCHER_ROOT", "").strip()
    if env_root:
        candidates.append(Path(env_root))
    executable_root = Path(sys.executable).resolve().parent
    script_root = Path(__file__).resolve().parent
    candidates.extend(
        [executable_root, executable_root.parent, script_root, script_root.parent]
    )
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if is_source_root(resolved):
            return resolved
    raise LauncherError(
        "启动文件不完整：请确认启动器旁边存在 app、static 和 requirements.txt。"
    )


def default_runtime_root() -> Path:
    override = os.environ.get("INKFORGE_LAUNCHER_RUNTIME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".inkforge")))
    return (local / "InkForge" / "runtime").resolve()


def _run_checked(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
        raise LauncherError(str(detail).strip()) from exc


def validate_python(command: list[str]) -> str | None:
    probe = (
        "import json,sys; print(json.dumps({'version':list(sys.version_info[:3]),"
        "'executable':sys.executable}))"
    )
    try:
        result = subprocess.run(
            [*command, "-c", probe],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        version = tuple(int(value) for value in payload["version"])
        if version[:2] >= MINIMUM_PYTHON and version[:2] < (4, 0):
            return str(payload["executable"])
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    return None


class Bootstrapper:
    def __init__(
        self,
        options: LaunchOptions,
        report: Callable[[str, float | None], None] | None = None,
    ):
        self.options = options
        self.report = report or (lambda _message, _progress=None: None)
        self.runtime_root = options.runtime_root
        self.download_root = self.runtime_root / "downloads"
        self.log_root = self.runtime_root.parent / "logs"
        self.log_path = self.log_root / "launcher.log"

    def status(self, message: str, progress: float | None = None) -> None:
        self.log_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
        self.report(message, progress)

    def _python_candidates(self) -> list[list[str]]:
        candidates: list[list[str]] = []
        override = os.environ.get("INKFORGE_BOOTSTRAP_PYTHON", "").strip()
        if override:
            candidates.append([override])
        managed = self.runtime_root / "Python312" / "python.exe"
        candidates.append([str(managed)])
        py_launcher = shutil.which("py")
        if py_launcher:
            candidates.append([py_launcher, "-3.12"])
            candidates.append([py_launcher, "-3"])
        python = shutil.which("python")
        if python and (not getattr(sys, "frozen", False) or Path(python) != Path(sys.executable)):
            candidates.append([python])
        return candidates

    def find_python(self) -> list[str] | None:
        for command in self._python_candidates():
            executable = validate_python(command)
            if executable:
                self.status(f"已找到兼容的 Python：{executable}", 0.12)
                return command
        return None

    def _download(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.unlink(missing_ok=True)
        request = Request(url, headers={"User-Agent": f"InkForgeLauncher/{APP_VERSION}"})
        try:
            with urlopen(request, timeout=30) as response, temporary.open("wb") as target:
                total = int(response.headers.get("Content-Length", "0") or 0)
                received = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    received += len(chunk)
                    if total:
                        self.report(
                            f"正在下载 Python 运行环境（{received * 100 // total}%）",
                            0.15 + 0.35 * received / total,
                        )
        except (OSError, URLError) as exc:
            temporary.unlink(missing_ok=True)
            raise LauncherError(f"Python 下载失败：{exc}") from exc
        os.replace(temporary, destination)

    def install_python(self) -> list[str]:
        installer = self.download_root / f"python-{PYTHON_VERSION}-amd64.exe"
        self.status("未找到兼容环境，正在准备 Python 运行环境……", 0.15)
        if not installer.is_file() or sha256_file(installer) != PYTHON_INSTALLER_SHA256:
            installer.unlink(missing_ok=True)
            self._download(PYTHON_INSTALLER_URL, installer)
        if sha256_file(installer) != PYTHON_INSTALLER_SHA256:
            installer.unlink(missing_ok=True)
            raise LauncherError("Python 安装包完整性校验失败，已停止安装。")
        target = self.runtime_root / "Python312"
        self.status("下载校验通过，正在安装独立运行环境……", 0.52)
        completed = subprocess.run(
            [
                str(installer),
                "/quiet",
                "InstallAllUsers=0",
                f"TargetDir={target}",
                "Include_pip=1",
                "Include_launcher=0",
                "Include_test=0",
                "Shortcuts=0",
                "PrependPath=0",
            ],
            check=False,
        )
        if completed.returncode not in (0, 3010):
            raise LauncherError(f"Python 安装失败，退出码：{completed.returncode}")
        command = [str(target / "python.exe")]
        if not validate_python(command):
            raise LauncherError("Python 安装完成，但运行环境自检失败。")
        return command

    def ensure_environment(self, python_command: list[str]) -> Path:
        fingerprint = requirements_fingerprint(self.options.source_root)
        environment = self.runtime_root / f"venv-{APP_VERSION}-{fingerprint[:12]}"
        python = environment / "Scripts" / "python.exe"
        marker = environment / "inkforge-requirements.sha256"
        if python.is_file() and marker.is_file() and marker.read_text().strip() == fingerprint:
            self.status("依赖环境已就绪，无需重复下载。", 0.82)
            return python
        if environment.exists():
            shutil.rmtree(environment)
        self.status("正在创建砚火专用环境……", 0.58)
        _run_checked([*python_command, "-m", "venv", str(environment)], capture_output=True)
        self.status("正在下载并安装项目依赖，首次启动需要几分钟……", 0.68)
        _run_checked(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--requirement",
                str(self.options.source_root / "requirements.txt"),
            ],
            cwd=str(self.options.source_root),
            capture_output=True,
        )
        marker.write_text(fingerprint, encoding="ascii")
        self.status("项目依赖安装完成。", 0.86)
        return python

    def _health(self, url: str, timeout: float = 1.5) -> bool:
        try:
            with urlopen(url.rstrip("/") + "/api/health", timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return response.status == 200 and payload.get("status") == "ok"
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            return False

    def _wait_for_health(self, url: str, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._health(url):
                return
            if process.poll() is not None:
                raise LauncherError(
                    f"砚火未能启动，请查看日志：{self.log_root / 'inkforge.log'}"
                )
            time.sleep(0.35)
        raise LauncherError("砚火启动超时，请检查端口和日志。")

    def launch(self, python: Path) -> LaunchResult:
        url = f"http://{self.options.host}:{self.options.port}/"
        if self._health(url):
            self.status("砚火已经在运行，正在打开工作台。", 1.0)
            if self.options.open_browser:
                webbrowser.open(url)
            return LaunchResult(url=url, reused_server=True)
        self.status("正在执行升级前检查……", 0.89)
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["INKFORGE_LOG_PATH"] = str(self.log_root / "inkforge.log")
        data_root = self.runtime_root.parent / "data"
        environment.setdefault("INKFORGE_DB_PATH", str(data_root / "inkforge.db"))
        environment.setdefault(
            "INKFORGE_SECRET_PATH", str(data_root / "inkforge-secrets.db")
        )
        environment.setdefault("INKFORGE_BACKUP_PATH", str(data_root / "backups"))
        preflight = self.options.source_root / "scripts" / "preflight_upgrade.py"
        if preflight.is_file():
            _run_checked(
                [str(python), str(preflight)],
                cwd=str(self.options.source_root),
                capture_output=True,
                env=environment,
            )
        self.status("正在启动砚火工作台……", 0.93)
        command = [
            str(python),
            "-m",
            "app",
            "--host",
            self.options.host,
            "--port",
            str(self.options.port),
            "--no-browser",
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        process_log = (self.log_root / "process.log").open("ab")
        process = subprocess.Popen(
            command,
            cwd=str(self.options.source_root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=process_log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            self._wait_for_health(url, process)
        except Exception:
            process.terminate()
            process_log.close()
            raise
        process_log.close()
        self.status("启动完成。", 1.0)
        if self.options.open_browser:
            webbrowser.open(url)
        if self.options.smoke:
            process.terminate()
            process.wait(timeout=15)
        return LaunchResult(url=url, reused_server=False, process_id=process.pid)

    def run(self) -> LaunchResult:
        if not is_source_root(self.options.source_root):
            raise LauncherError("部署目录不完整。")
        self.status("正在检查运行环境……", 0.05)
        if self.options.dry_run:
            self.status("部署文件和启动配置检查通过。", 1.0)
            return LaunchResult(
                url=f"http://{self.options.host}:{self.options.port}/",
                reused_server=False,
            )
        python_command = self.find_python() or self.install_python()
        python = self.ensure_environment(python_command)
        return self.launch(python)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="砚火自动部署启动器")
    parser.add_argument("--source-root", default="")
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--host", default=os.environ.get("INKFORGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("INKFORGE_PORT", "7860")))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args(argv)


def options_from_args(args: argparse.Namespace) -> LaunchOptions:
    return LaunchOptions(
        source_root=discover_source_root(args.source_root),
        runtime_root=(
            Path(args.runtime_root).expanduser().resolve()
            if args.runtime_root
            else default_runtime_root()
        ),
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        dry_run=args.dry_run,
        smoke=args.smoke,
    )


def safe_console_write(stream: object, message: str) -> None:
    """Best-effort diagnostics for both console and PyInstaller windowed builds."""
    if stream is None:
        return
    try:
        stream.write(message + "\n")  # type: ignore[attr-defined]
        stream.flush()  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        # A --windowed executable can expose a placeholder stream backed by an
        # invalid Windows handle. Logging already persisted the same status.
        return


def run_headless(options: LaunchOptions) -> int:
    def report(message: str, _progress: float | None = None) -> None:
        safe_console_write(sys.stdout, message)

    try:
        result = Bootstrapper(options, report).run()
        safe_console_write(sys.stdout, result.url)
        return 0
    except LauncherError as exc:
        safe_console_write(sys.stderr, f"启动失败：{exc}")
        return 1


def run_gui(options: LaunchOptions) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        return run_headless(options)

    root = tk.Tk()
    root.title("砚火 · 自动部署启动器")
    root.geometry("620x330")
    root.minsize(560, 300)
    root.configure(bg="#0b1220")
    events: queue.Queue[tuple[str, object]] = queue.Queue()

    tk.Label(
        root,
        text="砚火  INKFORGE",
        font=("Microsoft YaHei UI", 22, "bold"),
        fg="#f7efe4",
        bg="#0b1220",
    ).pack(anchor="w", padx=38, pady=(34, 4))
    tk.Label(
        root,
        text="本地 AI 小说创作工作台 · 环境检测与自动部署",
        font=("Microsoft YaHei UI", 10),
        fg="#91a4be",
        bg="#0b1220",
    ).pack(anchor="w", padx=40)
    status_var = tk.StringVar(value="正在初始化……")
    status = tk.Label(
        root,
        textvariable=status_var,
        font=("Microsoft YaHei UI", 11),
        fg="#dce8f6",
        bg="#0b1220",
        wraplength=535,
        justify="left",
    )
    status.pack(anchor="w", padx=40, pady=(48, 14))
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        "InkForge.Horizontal.TProgressbar",
        troughcolor="#19263a",
        background="#d26a4a",
        bordercolor="#19263a",
        lightcolor="#d26a4a",
        darkcolor="#d26a4a",
        thickness=12,
    )
    progress = ttk.Progressbar(
        root, style="InkForge.Horizontal.TProgressbar", maximum=100, length=540
    )
    progress.pack(padx=40, fill="x")
    detail_var = tk.StringVar(value="首次启动会自动准备运行环境；以后可直接进入。")
    tk.Label(
        root,
        textvariable=detail_var,
        font=("Microsoft YaHei UI", 9),
        fg="#6f829c",
        bg="#0b1220",
    ).pack(anchor="w", padx=40, pady=(12, 0))

    def report(message: str, value: float | None) -> None:
        events.put(("status", (message, value)))

    def worker() -> None:
        try:
            events.put(("done", Bootstrapper(options, report).run()))
        except Exception as exc:
            events.put(("error", exc))

    def poll() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "status":
                    message, value = payload  # type: ignore[misc]
                    status_var.set(str(message))
                    if value is not None:
                        progress["value"] = float(value) * 100
                elif kind == "done":
                    detail_var.set("工作台已在浏览器中打开，本窗口即将关闭。")
                    root.after(1300, root.destroy)
                else:
                    detail_var.set(f"日志位置：{options.runtime_root.parent / 'logs'}")
                    messagebox.showerror("砚火启动失败", str(payload), parent=root)
        except queue.Empty:
            pass
        if root.winfo_exists():
            root.after(100, poll)

    threading.Thread(target=worker, daemon=True).start()
    root.after(100, poll)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        options = options_from_args(args)
    except LauncherError as exc:
        if args.headless:
            safe_console_write(sys.stderr, f"启动失败：{exc}")
            return 1
        try:
            from tkinter import messagebox

            messagebox.showerror("砚火启动失败", str(exc))
        except ImportError:
            safe_console_write(sys.stderr, f"启动失败：{exc}")
        return 1
    if args.headless:
        return run_headless(options)
    return run_gui(options)


if __name__ == "__main__":
    raise SystemExit(main())
