#!/usr/bin/env python3
# Copyright 2026 Froncort
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DNO no-code UI — stdlib-only local launcher and API server."""

from __future__ import annotations

import json
import mimetypes
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import venv
import webbrowser
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parent
VENV_DIR = REPO_ROOT / ".venv"
RESULTS_DIR = REPO_ROOT / "results"
HOST = "127.0.0.1"
PORT = 8765
ACTIVE_PORT = PORT
SESSION_TOKEN = secrets.token_urlsafe(32)

# Stream optimize/setup job output to the terminal (set by start-dno-ui.sh).
TERMINAL_LOG = os.environ.get("DNO_UI_LOG", "").lower() in ("1", "true", "yes")

ALLOWED_FOCUS = frozenset({"util", "access", "cost", "balanced"})
ALLOWED_ADMIN_LEVEL = frozenset({"admin_area_1", "admin_area_2"})


@dataclass
class Job:
    job_id: str
    kind: str
    argv: list[str]
    cwd: Path
    lines: list[str] = field(default_factory=list)
    done: bool = False
    exit_code: Optional[int] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    process: Optional[subprocess.Popen[str]] = None
    subscribers: list[threading.Event] = field(default_factory=list)


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminal_log(message: str, *, kind: str = "job") -> None:
    """Mirror UI job output to the shell when DNO_UI_LOG is set."""
    if not TERMINAL_LOG:
        return
    print(f"[{kind}] {message}", flush=True)


def _python_version_tuple() -> tuple[int, ...]:
    return sys.version_info[:3]


def _venv_python() -> Optional[Path]:
    if platform.system() == "Windows":
        candidate = VENV_DIR / "Scripts" / "python.exe"
    else:
        candidate = VENV_DIR / "bin" / "python"
    return candidate if candidate.is_file() else None


def _resolve_python() -> Path:
    venv_py = _venv_python()
    if venv_py is not None:
        return venv_py
    return Path(sys.executable)


def _safe_path(raw: str) -> Path:
    if not raw or not str(raw).strip():
        raise ValueError("path is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (REPO_ROOT / candidate).resolve()
    else:
        candidate = candidate.resolve()
    repo = REPO_ROOT.resolve()
    try:
        candidate.relative_to(repo)
    except ValueError as exc:
        raise ValueError("path must stay inside the dno-core folder") from exc
    return candidate


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        backup = path.with_suffix(path.suffix + f".{int(time.time())}.bak")
        shutil.copy2(path, backup)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _check_dno_installed(python_exe: Path) -> bool:
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", "import dno_core; print('ok')"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0 and "ok" in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _check_cbc(python_exe: Path) -> bool:
    try:
        proc = subprocess.run(
            [
                str(python_exe),
                "-c",
                "import os, shutil, pulp; "
                "p = os.environ.get('DNO_CBC_PATH') or shutil.which('cbc'); "
                "s = pulp.COIN_CMD(path=p) if p else pulp.PULP_CBC_CMD(msg=False); "
                "print('ok' if s.available() else 'no')",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0 and "ok" in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _list_samples() -> list[dict[str, Any]]:
    """Public OSS ships one sample demo under samples/demo/."""
    return [
        {
            "key": "demo",
            "label": "Sample network",
            "network": "samples/demo/network.json",
            "catalogue": "samples/demo/network.catalogue.json",
            "expect_check_only": "pass",
            "note": "Bundled demo network for Open Referral.",
        }
    ]


def _list_runs() -> list[dict[str, Any]]:
    if not RESULTS_DIR.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for child in sorted(RESULTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        manifest = child / "run-manifest.json"
        summary = child / "summary.md"
        baseline = child / "baseline.json"
        validation = child / "validation-report.md"
        result = child / "result.json"
        runs.append(
            {
                "id": child.name,
                "path": str(child.relative_to(REPO_ROOT)),
                "modified": datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc).isoformat(),
                "has_summary": summary.is_file(),
                "has_baseline": baseline.is_file(),
                "has_validation": validation.is_file(),
                "has_result": result.is_file(),
                "has_manifest": manifest.is_file(),
            }
        )
    return runs


def _build_status() -> dict[str, Any]:
    py = _resolve_python()
    venv_exists = VENV_DIR.is_dir()
    dno_installed = _check_dno_installed(py) if venv_exists else False
    cbc_ok = _check_cbc(py) if dno_installed else False
    version = _python_version_tuple()
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "python": {
            "executable": str(py),
            "version": ".".join(str(v) for v in version),
            "ok": version >= (3, 10),
        },
        "venv": {
            "exists": venv_exists,
            "path": str(VENV_DIR.relative_to(REPO_ROOT)),
        },
        "dno_installed": dno_installed,
        "cbc_available": cbc_ok,
        "ready": dno_installed and cbc_ok and version >= (3, 10),
        "repo_root": str(REPO_ROOT),
        "samples": _list_samples(),
        "runs": _list_runs(),
        "token": SESSION_TOKEN,
        "port": ACTIVE_PORT,
    }


def _dno_argv_base() -> list[str]:
    py = _resolve_python()
    return [str(py), "-u", "-m", "dno_core.cli"]


def _build_run_argv(action: str, options: dict[str, Any]) -> tuple[list[str], Path]:
    argv = _dno_argv_base()
    cwd = REPO_ROOT

    if action == "demo":
        argv.append("demo")
        argv.extend(["-g", "referral-network"])
        output = options.get("output") or f"results/demo-{int(time.time())}"
        argv.extend(["-o", output])
        _apply_common_flags(argv, options)
        return argv, cwd

    if action == "init":
        directory = options.get("directory")
        if not directory:
            raise ValueError("directory is required for init")
        target = _safe_path(directory)
        argv.append("init")
        argv.append(str(target.relative_to(REPO_ROOT)))
        argv.extend(["-g", "referral-network"])
        if options.get("empty"):
            argv.append("--empty")
        if options.get("force"):
            argv.append("--force")
        return argv, cwd

    if action == "optimize":
        network = options.get("network")
        if not network:
            raise ValueError("network path is required")
        network_path = _safe_path(network)
        argv.append("optimize")
        argv.extend(["-n", str(network_path.relative_to(REPO_ROOT))])
        catalogue = options.get("catalogue")
        if catalogue:
            catalogue_path = _safe_path(catalogue)
            if catalogue_path.is_file():
                argv.extend(["-c", str(catalogue_path.relative_to(REPO_ROOT))])
        argv.extend(["-g", "referral-network"])
        if options.get("check_only"):
            argv.append("--check-only")
        if options.get("baseline_only"):
            argv.append("--baseline-only")
        focus = options.get("focus")
        if focus:
            if focus not in ALLOWED_FOCUS:
                raise ValueError(f"invalid focus: {focus}")
            argv.extend(["--focus", focus])
        if options.get("partition_by_admin_area"):
            argv.append("--partition-by-admin-area")
            admin_level = options.get("admin_area_level", "admin_area_1")
            if admin_level not in ALLOWED_ADMIN_LEVEL:
                raise ValueError(f"invalid admin_area_level: {admin_level}")
            argv.extend(["--admin-area-level", admin_level])
        if options.get("allow_synthetic_referrals"):
            argv.append("--allow-synthetic-referrals")
        output = options.get("output")
        if output:
            argv.extend(["-o", str(_safe_path(output).relative_to(REPO_ROOT))])
        _apply_common_flags(argv, options)
        return argv, cwd

    raise ValueError(f"unknown action: {action}")


def _apply_common_flags(argv: list[str], options: dict[str, Any]) -> None:
    if options.get("quiet"):
        argv.append("-q")
    if options.get("verbose"):
        argv.append("-v")


def _start_job(kind: str, argv: list[str], cwd: Path) -> Job:
    job_id = uuid.uuid4().hex
    job = Job(job_id=job_id, kind=kind, argv=argv, cwd=cwd)
    with JOBS_LOCK:
        JOBS[job_id] = job

    def _run() -> None:
        _run_argv_sequence(job, [argv], cwd)

    threading.Thread(target=_run, daemon=True).start()
    return job


def _start_setup_job() -> Job:
    if not VENV_DIR.is_dir():
        builder = venv.EnvBuilder(with_pip=True, clear=False)
        builder.create(VENV_DIR)
    venv_py = _venv_python()
    if venv_py is None:
        raise RuntimeError("failed to create virtual environment")
    commands = [
        [str(venv_py), "-m", "pip", "install", "--upgrade", "pip"],
        [str(venv_py), "-m", "pip", "install", "-e", str(REPO_ROOT)],
    ]
    job_id = uuid.uuid4().hex
    job = Job(job_id=job_id, kind="setup", argv=commands[-1], cwd=REPO_ROOT)
    with JOBS_LOCK:
        JOBS[job_id] = job

    def _run() -> None:
        _run_argv_sequence(job, commands, REPO_ROOT)

    threading.Thread(target=_run, daemon=True).start()
    return job


def _run_argv_sequence(job: Job, commands: list[list[str]], cwd: Path) -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    final_code = 0
    _terminal_log(f"started ({job.kind})", kind=job.kind)
    try:
        for argv in commands:
            cmd_line = f"$ {' '.join(argv)}"
            with job.lock:
                job.lines.append(cmd_line)
                for event in job.subscribers:
                    event.set()
            _terminal_log(cmd_line, kind=job.kind)
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            with job.lock:
                job.process = proc
            if proc.stdout is not None:
                for line in proc.stdout:
                    stripped = line.rstrip("\n")
                    with job.lock:
                        job.lines.append(stripped)
                        for event in job.subscribers:
                            event.set()
                    if stripped:
                        _terminal_log(stripped, kind=job.kind)
            code = proc.wait()
            if code != 0:
                final_code = code
                break
        with job.lock:
            job.exit_code = final_code
            job.done = True
            job.process = None
            for event in job.subscribers:
                event.set()
        status = "done" if final_code == 0 else f"exit {final_code}"
        _terminal_log(status, kind=job.kind)
    except Exception as exc:  # noqa: BLE001
        err = f"error: {type(exc).__name__}: {exc}"
        with job.lock:
            job.lines.append(err)
            job.exit_code = 1
            job.done = True
            job.process = None
            for event in job.subscribers:
                event.set()
        _terminal_log(err, kind=job.kind)


def _cancel_job(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return False
    with job.lock:
        proc = job.process
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True
    return False


def _reveal_path(path: Path) -> None:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(path)], cwd=REPO_ROOT)  # noqa: S603
    elif system == "Windows":
        subprocess.Popen(["explorer", str(path)])  # noqa: S603
    else:
        subprocess.Popen(["xdg-open", str(path)], cwd=REPO_ROOT)  # noqa: S603


def _zip_directory(path: Path) -> Path:
    archive = path.with_suffix(".zip")
    if archive.is_file():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in path.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(path))
    return archive


class DnoUiHandler(BaseHTTPRequestHandler):
    server_version = "DnoUi/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON body") from exc

    def _check_api_auth(self, qs: Optional[dict[str, list[str]]] = None) -> bool:
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith(f"http://{HOST}") and not origin.startswith(f"http://localhost"):
            self._send_json({"error": "forbidden origin"}, HTTPStatus.FORBIDDEN)
            return False
        token = self.headers.get("X-DNO-Token", "")
        if not token and qs:
            token = (qs.get("token") or [""])[0]
        if token != SESSION_TOKEN:
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def _serve_static(self, rel_path: str) -> None:
        if rel_path in ("", "/", "/index.html"):
            rel_path = "index.html"
        else:
            rel_path = rel_path.lstrip("/")
        file_path = (REPO_ROOT / rel_path).resolve()
        try:
            file_path.relative_to(REPO_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = file_path.read_bytes()
        mime, _ = mimetypes.guess_type(str(file_path))
        self._send_bytes(data, mime or "application/octet-stream")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path.startswith("/api/"):
            qs = parse_qs(parsed.query)
            if path != "/api/status" and not self._check_api_auth(qs):
                return
            try:
                if path == "/api/status":
                    self._send_json(_build_status())
                elif path == "/api/stream":
                    job_id = (qs.get("job") or [""])[0]
                    self._handle_stream(job_id)
                elif path == "/api/network":
                    self._handle_get_network(qs)
                elif path == "/api/results":
                    self._send_json({"runs": _list_runs()})
                elif path == "/api/file":
                    self._handle_get_file(qs)
                elif path == "/api/zip":
                    self._handle_zip(qs)
                else:
                    self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._check_api_auth():
            return
        try:
            body = self._read_body()
            if path == "/api/setup":
                job = _start_setup_job()
                self._send_json({"job_id": job.job_id, "argv": job.argv})
            elif path == "/api/run":
                action = body.get("action", "")
                options = body.get("options") or {}
                argv, cwd = _build_run_argv(action, options)
                job = _start_job(action, argv, cwd)
                self._send_json({"job_id": job.job_id, "argv": job.argv})
            elif path == "/api/cancel":
                job_id = body.get("job_id", "")
                ok = _cancel_job(job_id)
                self._send_json({"cancelled": ok})
            elif path == "/api/network":
                self._handle_post_network(body)
            elif path == "/api/reveal":
                target = _safe_path(body.get("path", ""))
                _reveal_path(target)
                self._send_json({"ok": True})
            elif path == "/api/remove-venv":
                if VENV_DIR.is_dir():
                    shutil.rmtree(VENV_DIR)
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle_stream(self, job_id: str) -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if job is None:
            self._send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        sent = 0
        while True:
            with job.lock:
                lines = job.lines[sent:]
                done = job.done
                exit_code = job.exit_code
            for line in lines:
                payload = json.dumps({"type": "line", "text": line})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                sent += 1
            if done:
                payload = json.dumps({"type": "done", "exit_code": exit_code})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                break
            event = threading.Event()
            with job.lock:
                job.subscribers.append(event)
            event.wait(timeout=1.0)
            with job.lock:
                if event in job.subscribers:
                    job.subscribers.remove(event)

    def _handle_get_network(self, qs: dict[str, list[str]]) -> None:
        network = (qs.get("network") or [""])[0]
        if not network:
            raise ValueError("network query parameter required")
        network_path = _safe_path(network)
        if not network_path.is_file():
            raise ValueError("network file not found")
        payload: dict[str, Any] = {
            "network": _read_json(network_path),
            "network_path": str(network_path.relative_to(REPO_ROOT)),
        }
        catalogue_raw = (qs.get("catalogue") or [""])[0].strip()
        catalogue_path: Optional[Path] = None
        if catalogue_raw:
            catalogue_path = _safe_path(catalogue_raw)
        elif (qs.get("catalogue_path") or [""])[0].strip():
            catalogue_path = _safe_path((qs.get("catalogue_path") or [""])[0])
        if catalogue_path is None:
            catalogue_path = network_path.with_name("network.catalogue.json")
            if not catalogue_path.is_file():
                alt = network_path.parent / f"{network_path.stem}.catalogue.json"
                if alt.is_file():
                    catalogue_path = alt
                else:
                    catalogue_path = None
        elif not catalogue_path.is_file():
            raise ValueError("catalogue file not found")
        if catalogue_path is not None and catalogue_path.is_file():
            payload["catalogue"] = _read_json(catalogue_path)
            payload["catalogue_path"] = str(catalogue_path.relative_to(REPO_ROOT))
        self._send_json(payload)

    def _handle_post_network(self, body: dict[str, Any]) -> None:
        network_path = _safe_path(body.get("network_path", ""))
        network_data = body.get("network")
        if network_data is None:
            raise ValueError("network payload required")
        _write_json(network_path, network_data)
        catalogue_data = body.get("catalogue")
        catalogue_path_raw = body.get("catalogue_path")
        if catalogue_data is not None:
            if catalogue_path_raw:
                catalogue_path = _safe_path(catalogue_path_raw)
            else:
                catalogue_path = network_path.with_name("network.catalogue.json")
            _write_json(catalogue_path, catalogue_data)
        self._send_json({"ok": True})

    def _handle_get_file(self, qs: dict[str, list[str]]) -> None:
        rel = (qs.get("path") or [""])[0]
        file_path = _safe_path(rel)
        if not file_path.is_file():
            raise ValueError("file not found")
        mime, _ = mimetypes.guess_type(str(file_path))
        data = file_path.read_bytes()
        self._send_bytes(data, mime or "application/octet-stream")

    def _handle_zip(self, qs: dict[str, list[str]]) -> None:
        rel = (qs.get("path") or [""])[0]
        dir_path = _safe_path(rel)
        if not dir_path.is_dir():
            raise ValueError("directory not found")
        archive = _zip_directory(dir_path)
        data = archive.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{archive.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _bind_server() -> ThreadingHTTPServer:
    global ACTIVE_PORT
    last_error: Optional[OSError] = None
    for port in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer((HOST, port), DnoUiHandler)
            server.allow_reuse_address = True
            ACTIVE_PORT = port
            return server
        except OSError as exc:
            last_error = exc
    raise SystemExit(
        f"Could not start DNO UI on ports {PORT}-{PORT + 9}. "
        "Another copy may already be running — close it or wait a moment."
    ) from last_error


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    server = _bind_server()
    url = f"http://{HOST}:{ACTIVE_PORT}/"
    print(f"DNO UI running at {url}")
    if ACTIVE_PORT != PORT:
        print(f"(Port {PORT} was busy — using {ACTIVE_PORT} instead.)")
    print(f"Session token: {SESSION_TOKEN}")
    if TERMINAL_LOG:
        print("Job logs stream below when you install tools or run analyses in the browser.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
