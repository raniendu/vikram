"""Explicit extension packages and isolated host worker lifecycle.

Extensions are trusted host code, not sandboxes. Only user-command adapters may
call install/update/remove/enable. Model tools use call_tool and its approval
check; project settings never install or enable packages.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from vikram.coding import child_environment
from vikram.documents import ensure_space
from vikram.extension_sdk import API_VERSION, MAX_FRAME, NAME, validate_value
from vikram.interaction import ActionRequest, Approver, argument_preview
from vikram.settings import Settings, _contains_credentials

MAX_REGISTRY = 1024 * 1024
MAX_PACKAGES = 32
MAX_CALLS = 512
APPROVAL_TIMEOUT = 15 * 60
REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9_.+*,-]+)*\Z"
)


def runtime_python() -> Path:
    """A frozen app must use its pinned interpreter, never a system fallback."""
    if getattr(sys, "frozen", False):
        executable = (
            Path(sys.executable).parent.parent
            / "Resources/extension-python/bin/python3"
        )
        if not executable.is_file():
            raise ValueError(
                "The bundled extension interpreter is missing. Reinstall Vikram."
            )
        return executable
    return Path(sys.executable)


def _worker_sources() -> Path:
    if getattr(sys, "frozen", False):
        location = (
            Path(sys.executable).parent.parent / "Resources/extension-worker/vikram"
        )
    else:
        location = Path(__file__).parent
    if not all(
        (location / name).is_file()
        for name in ("extension_sdk.py", "extension_worker.py")
    ):
        raise ValueError("The extension worker files are missing. Reinstall Vikram.")
    return location


def _environment() -> dict[str, str]:
    environment = child_environment()
    environment.pop("VIRTUAL_ENV", None)
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1",
        }
    )
    return environment


async def _terminate(process) -> None:
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        pass
    # The leader may already have exited while its descendants remain alive.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _run(arguments: list[str], root: Path, *, timeout: float = 300) -> None:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=_environment(),
        start_new_session=True,
        cwd=root,
    )
    waiting = asyncio.create_task(process.wait())
    try:
        async with asyncio.timeout(timeout):
            while process.returncode is None:
                ensure_space(root)
                try:
                    await asyncio.wait_for(asyncio.shield(waiting), 1)
                except asyncio.TimeoutError:
                    continue
        if process.returncode:
            raise ValueError(
                "Extension installation failed. The previous installation is unchanged."
            )
    except TimeoutError:
        raise ValueError(
            "Extension installation timed out. The previous installation is unchanged."
        ) from None
    finally:
        await _terminate(process)
        await waiting


class _Worker:
    def __init__(self, environment: Path):
        self.environment = environment
        self.process = None
        self.lock = asyncio.Lock()
        self.description: dict = {}
        self.lease: int | None = None

    async def start(self) -> None:
        python = self.environment / "bin/python3"
        if not python.is_file():
            raise ValueError(
                "This extension environment is unavailable. Reinstall the extension."
            )
        self.lease = os.open(
            self.environment / ".in-use", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(self.lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.process = await asyncio.create_subprocess_exec(
                str(python),
                "-I",
                "-u",
                "-m",
                "vikram.extension_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_environment(),
                start_new_session=True,
                limit=MAX_FRAME + 1,
                cwd=self.environment,
            )
        except BaseException:
            os.close(self.lease)
            self.lease = None
            raise

    async def request(self, method: str, params: dict, *, timeout: float = 300):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                raise ValueError(
                    "The extension worker is stopped. Reload extensions to restart it."
                )
            request_id = uuid4().hex
            raw = (
                json.dumps(
                    {
                        "v": API_VERSION,
                        "id": request_id,
                        "method": method,
                        "params": params,
                    },
                    allow_nan=False,
                ).encode()
                + b"\n"
            )
            if len(raw) > MAX_FRAME:
                raise ValueError("This extension request exceeds the frame limit.")
            try:
                async with asyncio.timeout(timeout):
                    self.process.stdin.write(raw)
                    await self.process.stdin.drain()
                    line = await self.process.stdout.readline()
                if not line or len(line) > MAX_FRAME:
                    raise ValueError(
                        "The extension worker returned an invalid response."
                    )
                response = json.loads(line)
                if response.get("v") != API_VERSION or response.get("id") != request_id:
                    raise ValueError(
                        "The extension worker returned an invalid response."
                    )
                if response.get("error"):
                    raise ValueError(
                        "The extension operation failed; private exception details were omitted."
                    )
                return response["result"]
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            await _terminate(process)
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None


class ExtensionManager:
    def __init__(self, settings: Settings, interaction=None, mode: str = "normal"):
        self.settings, self.interaction, self.mode = settings, interaction, mode
        self.root = settings.root / "extensions"
        self.environments = self.root / "environments"
        self._workers: dict[str, _Worker] = {}
        self._errors: dict[str, str] = {}
        self._calls: dict[str, tuple[str, asyncio.Task]] = {}
        self._started = False

    def _read(self) -> dict:
        path = self.root / "registry.json"
        if not path.exists():
            return {"version": 1, "packages": {}}
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_REGISTRY + 1)
            record = json.loads(raw)
            if (
                len(raw) > MAX_REGISTRY
                or record.get("version") != 1
                or not isinstance(record.get("packages"), dict)
                or len(record["packages"]) > MAX_PACKAGES
            ):
                raise ValueError()
            for name, package in record["packages"].items():
                if (
                    not NAME.fullmatch(name)
                    or not isinstance(package, dict)
                    or package.get("name") != name
                    or not re.fullmatch(r"[0-9a-f]{32}", package.get("environment", ""))
                ):
                    raise ValueError()
            return record
        except (OSError, ValueError, TypeError, AttributeError):
            raise ValueError(
                "The extension registry is invalid. Restore its previous copy before installing packages."
            ) from None

    @contextmanager
    def _locked(self):
        self.environments.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            self.root / "registry.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Another Vikram instance is changing extension packages."
                ) from None
            yield
        finally:
            os.close(fd)

    def _save(self, record: dict) -> None:
        encoded = json.dumps(record, ensure_ascii=False, indent=2).encode()
        if len(encoded) > MAX_REGISTRY:
            raise ValueError("The extension registry exceeds its size limit.")
        fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / "registry.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _path(self, record: dict) -> Path:
        path = self.environments / record["environment"]
        if path.is_symlink() or not path.resolve().is_relative_to(
            self.environments.resolve()
        ):
            raise ValueError("The extension environment path is invalid.")
        return path

    def status(self) -> list[dict]:
        result = []
        for name, package in self._read()["packages"].items():
            worker = self._workers.get(name)
            running = (
                worker is not None
                and worker.process is not None
                and worker.process.returncode is None
            )
            result.append(
                {
                    "name": name,
                    "source": package["source"],
                    "version": package["version"],
                    "enabled": package["enabled"],
                    "status": (
                        "running"
                        if running
                        else (
                            "failed"
                            if name in self._errors
                            else "installed" if package["enabled"] else "disabled"
                        )
                    ),
                    "error": self._errors.get(name),
                }
            )
        return sorted(result, key=lambda item: item["name"])

    async def _launch(self, record: dict) -> _Worker:
        worker = _Worker(self._path(record))
        try:
            await worker.start()
            description = await worker.request(
                "initialize",
                {
                    "entry": record["entry"],
                    "package": (
                        str(self._path(record) / "package")
                        if record.get("local")
                        else None
                    ),
                    "settings": record.get("settings", {}),
                    "workspace": (
                        str(self.settings.workspace)
                        if self.settings.workspace and self.settings.is_trusted()
                        else None
                    ),
                },
                timeout=20,
            )
            if (
                not isinstance(description, dict)
                or description.get("api_version") != API_VERSION
            ):
                raise ValueError("This extension uses an unsupported SDK version.")
            worker.description = description
            return worker
        except BaseException:
            await worker.close()
            raise

    async def start(self) -> list[dict]:
        if self.mode == "plan":
            return self.status()
        self._started = True
        for name, record in self._read()["packages"].items():
            if not record["enabled"] or name in self._workers:
                continue
            try:
                self._workers[name] = await self._launch(record)
                self._errors.pop(name, None)
            except Exception:
                self._errors[name] = (
                    "The extension worker could not start. Reinstall or disable it."
                )
        return self.status()

    async def close(self) -> None:
        tasks = [task for _, task in self._calls.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        workers, self._workers = self._workers, {}
        await asyncio.gather(
            *(worker.close() for worker in workers.values()), return_exceptions=True
        )
        self._started = False

    def _require_normal(self) -> None:
        if self.mode != "normal":
            raise ValueError(
                "Extensions run host code and are unavailable in plan mode."
            )

    def begin_reply(self) -> None:
        """Reset the budget only after all prior calls settle."""
        if any(not task.done() for _, task in self._calls.values()) or any(
            worker.lock.locked() for worker in self._workers.values()
        ):
            raise ValueError("An extension call is still running.")
        self._calls.clear()

    async def _venv(self, directory: Path) -> None:
        python = runtime_python()
        if getattr(sys, "frozen", False):
            # Venv base interpreters must outlive the installed app version.
            bundled = python.parent.parent
            try:
                pin = json.loads((bundled / "manifest.json").read_text())
                revision = pin["sha256"]
                if not re.fullmatch(r"[0-9a-f]{64}", revision):
                    raise ValueError()
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError(
                    "The bundled extension interpreter pin is invalid."
                ) from None
            runtimes = self.root / "runtimes"
            runtimes.mkdir(exist_ok=True, mode=0o700)
            persistent = runtimes / revision
            if not persistent.exists():
                ensure_space(self.root, 500 * 1024**2)
                temporary = runtimes / (".runtime-" + uuid4().hex)
                try:
                    shutil.copytree(bundled, temporary, symlinks=True)
                    temporary.rename(persistent)
                finally:
                    shutil.rmtree(temporary, ignore_errors=True)
            python = persistent / "bin/python3"
        await _run(
            [str(python), "-I", "-m", "venv", "--without-pip", str(directory)],
            self.root,
        )
        packages = list((directory / "lib").glob("python*/site-packages"))
        if len(packages) != 1:
            raise ValueError(
                "The extension interpreter could not create an isolated environment."
            )
        sdk = packages[0] / "vikram"
        sdk.mkdir()
        (sdk / "__init__.py").write_text('"""Private Vikram extension SDK."""\n')
        for name in ("extension_sdk.py", "extension_worker.py"):
            shutil.copy2(_worker_sources() / name, sdk / name)

    @staticmethod
    def _source(source: str) -> tuple[str, Path | None]:
        if (
            not isinstance(source, str)
            or not source
            or len(source) > 2000
            or any(char in source for char in "\n\r\x00")
        ):
            raise ValueError(
                "Specify a local extension directory, PyPI requirement, or Git HTTPS URL."
            )
        path = Path(source).expanduser()
        try:
            exists = path.exists()
        except OSError:
            exists = False
        if exists:
            path = path.resolve(strict=True)
            if path.is_dir() or path.suffix == ".whl":
                return str(path), path
            raise ValueError("Choose an extension directory or wheel.")
        if source.startswith("git+https://"):
            parsed = urlsplit(source[4:])
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Git extension URLs must use HTTPS without embedded credentials, queries, or fragments."
                )
            return source, None
        if REQUIREMENT.fullmatch(source):
            return source, None
        raise ValueError("Unsupported extension source; local paths must exist.")

    @staticmethod
    def _copy_local(source: Path, destination: Path) -> dict:
        manifest = source / "vikram-extension.json"
        if not manifest.is_file() or manifest.stat().st_size > 64_000:
            raise ValueError(
                "A local extension needs a bounded vikram-extension.json manifest."
            )
        try:
            record = json.loads(manifest.read_text())
        except (OSError, ValueError):
            raise ValueError("The local extension manifest is invalid.") from None
        if (
            not isinstance(record, dict)
            or record.get("api_version") != API_VERSION
            or not NAME.fullmatch(record.get("name", ""))
            or not isinstance(record.get("version"), str)
            or len(record["version"]) > 100
            or not isinstance(record.get("entry"), str)
        ):
            raise ValueError(
                "The local extension needs API version 1, a name, version, and entry point."
            )
        dependencies = record.get("dependencies", [])
        if (
            not isinstance(dependencies, list)
            or len(dependencies) > 64
            or any(
                not isinstance(item, str) or not REQUIREMENT.fullmatch(item)
                for item in dependencies
            )
        ):
            raise ValueError(
                "Extension dependencies must be explicit PyPI requirements."
            )
        total, count = 0, 0
        for directory, subdirs, files in os.walk(source):
            subdirs[:] = [
                name
                for name in subdirs
                if name not in {".git", ".venv", "__pycache__", "node_modules"}
            ]
            for name in [*subdirs, *files]:
                item = Path(directory) / name
                if item.is_symlink():
                    raise ValueError(
                        "Local extension packages cannot contain symlinks."
                    )
            for name in files:
                item = Path(directory) / name
                if not item.is_file():
                    raise ValueError(
                        "Local extension packages must contain regular files."
                    )
                total += item.stat().st_size
                count += 1
                if total > 20 * 1024 * 1024 or count > 2000:
                    raise ValueError(
                        "Local extension source exceeds 20 MiB or 2,000 files."
                    )
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", "node_modules"
            ),
        )
        return record

    async def _pip(self, directory: Path, sources: list[str]) -> None:
        python = str(directory / "bin/python3")
        await _run([python, "-I", "-m", "ensurepip", "--upgrade"], self.root)
        await _run(
            [
                python,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-compile",
                "--no-cache-dir",
                "--index-url",
                "https://pypi.org/simple",
                *sources,
            ],
            self.root,
        )

    async def install(self, source: str) -> dict:
        return await self._install(source)

    async def _install(self, source: str, *, replacing: str | None = None) -> dict:
        self._require_normal()
        source, local = self._source(source)
        with self._locked():
            registry = self._read()
            if replacing is None and len(registry["packages"]) >= MAX_PACKAGES:
                raise ValueError("Remove an extension before installing another.")
            ensure_space(self.root, 200 * 1024**2)
            revision = uuid4().hex
            directory = self.environments / revision
            candidate = None
            try:
                await self._venv(directory)
                if (
                    local is not None
                    and local.is_dir()
                    and (local / "vikram-extension.json").is_file()
                ):
                    manifest = self._copy_local(local, directory / "package")
                    if manifest.get("dependencies"):
                        await self._pip(directory, manifest["dependencies"])
                    name, entry, version = (
                        manifest["name"],
                        manifest["entry"],
                        manifest["version"],
                    )
                    is_local = True
                else:
                    await self._pip(directory, [source])
                    probe = _Worker(directory)
                    try:
                        await probe.start()
                        discovered = await probe.request("discover", {}, timeout=20)
                    finally:
                        await probe.close()
                    if not isinstance(discovered, list) or len(discovered) != 1:
                        raise ValueError(
                            "An extension distribution must publish exactly one vikram.extensions entry point."
                        )
                    name, entry, version = (
                        discovered[0]["name"],
                        discovered[0]["entry"],
                        discovered[0]["version"],
                    )
                    if not NAME.fullmatch(name):
                        raise ValueError("The extension entry-point name is invalid.")
                    is_local = False
                if replacing is not None and name != replacing:
                    raise ValueError(
                        "An extension update cannot change the package name."
                    )
                previous = registry["packages"].get(name)
                if previous and replacing is None:
                    raise ValueError(
                        "This extension is installed. Use update to replace it."
                    )
                record = {
                    "name": name,
                    "source": source,
                    "version": version,
                    "entry": entry,
                    "environment": revision,
                    "local": is_local,
                    "enabled": previous["enabled"] if previous else True,
                    "settings": previous.get("settings", {}) if previous else {},
                    "previous": (
                        [*previous.get("previous", []), previous["environment"]]
                        if previous
                        else []
                    ),
                }
                candidate = await self._launch(record)
                registry["packages"][name] = record
                self._save(registry)
            except BaseException:
                if candidate is not None:
                    await candidate.close()
                shutil.rmtree(directory, ignore_errors=True)
                raise
            old = self._workers.pop(name, None)
            if record["enabled"]:
                self._workers[name] = candidate
            else:
                await candidate.close()
            if old is not None:
                await old.close()
            self._errors.pop(name, None)
            self._started = True
        return next(item for item in self.status() if item["name"] == name)

    async def update(self, name: str) -> dict:
        package = self._read()["packages"].get(name)
        if package is None:
            raise ValueError("That extension is not installed.")
        return await self._install(package["source"], replacing=name)

    async def remove(self, name: str) -> None:
        self._require_normal()
        with self._locked():
            registry = self._read()
            record = registry["packages"].get(name)
            if record is None:
                raise ValueError("That extension is not installed.")
            worker = self._workers.pop(name, None)
            if worker is not None:
                await worker.close()
            descriptors, directories = [], []
            try:
                for revision in [record["environment"], *record.get("previous", [])]:
                    if isinstance(revision, str) and re.fullmatch(
                        r"[0-9a-f]{32}", revision
                    ):
                        directory = self.environments / revision
                        if directory.is_dir() and not directory.is_symlink():
                            fd = os.open(
                                directory / ".in-use", os.O_CREAT | os.O_RDWR, 0o600
                            )
                            descriptors.append(fd)
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                raise ValueError(
                                    "This extension is active in another Vikram instance. Close it there before removing it."
                                ) from None
                            directories.append(directory)
                registry["packages"].pop(name)
                self._save(registry)
                for directory in directories:
                    shutil.rmtree(directory)
            finally:
                for fd in descriptors:
                    os.close(fd)
            self._errors.pop(name, None)

    async def enable(self, name: str, enabled: bool) -> None:
        self._require_normal()
        if type(enabled) is not bool:
            raise ValueError("Choose enabled or disabled.")
        with self._locked():
            registry = self._read()
            if name not in registry["packages"]:
                raise ValueError("That extension is not installed.")
            record = registry["packages"][name]
            record["enabled"] = enabled
            self._save(registry)
        worker = self._workers.pop(name, None)
        if worker is not None:
            await worker.close()
        if enabled:
            try:
                self._workers[name] = await self._launch(record)
                self._errors.pop(name, None)
            except Exception:
                self._errors[name] = "The extension worker could not start."
                raise ValueError(self._errors[name]) from None

    async def list_tools(self) -> list[dict]:
        if not self._started:
            await self.start()
        if self.mode != "normal":
            return []
        result = []
        records = self._read()["packages"]
        for package, worker in self._workers.items():
            if (
                package not in records
                or not records[package]["enabled"]
                or worker.environment != self._path(records[package])
            ):
                continue
            if worker.process is None or worker.process.returncode is not None:
                continue
            for tool in worker.description.get("tools", []):
                result.append(
                    {
                        **tool,
                        "name": f"ext__{package}__{tool['name']}",
                        "schema": tool["input_schema"],
                    }
                )
        return result

    async def list_commands(self) -> list[dict]:
        if not self._started:
            await self.start()
        if self.mode != "normal":
            return []
        return [
            {**command, "name": f"ext__{package}__{command['name']}"}
            for package, worker in self._workers.items()
            for command in worker.description.get("commands", [])
        ]

    async def list_settings(self, name: str) -> dict:
        if not self._started:
            await self.start()
        worker = self._workers.get(name)
        if worker is None:
            raise ValueError("This extension is disabled or unavailable.")
        record = self._read()["packages"][name]
        return {
            key: {**spec, "value": record.get("settings", {}).get(key, spec["default"])}
            for key, spec in worker.description.get("settings", {}).items()
        }

    async def set_setting(self, name: str, key: str, value) -> None:
        self._require_normal()
        available = await self.list_settings(name)
        if key not in available:
            raise ValueError("Unknown extension setting.")
        if _contains_credentials({key: value}):
            raise ValueError("Credentials cannot be stored in extension settings.")
        validate_value(value, available[key]["schema"])
        with self._locked():
            registry = self._read()
            record = registry["packages"][name]
            record.setdefault("settings", {})[key] = value
            candidate = await self._launch(record)
            try:
                self._save(registry)
            except BaseException:
                await candidate.close()
                raise
            old = self._workers.get(name)
            self._workers[name] = candidate
            if old is not None:
                await old.close()

    @staticmethod
    def _parts(name: str) -> tuple[str, str]:
        parts = name.split("__")
        if (
            len(parts) != 3
            or parts[0] != "ext"
            or not all(NAME.fullmatch(part) for part in parts[1:])
        ):
            raise ValueError("Unknown extension operation.")
        return parts[1], parts[2]

    async def _approve(self, name: str, call_id: str, arguments: dict) -> bool:
        if not isinstance(self.interaction, Approver):
            return False
        request = ActionRequest(
            id=call_id,
            kind="extension",
            summary="Run extension operation " + name,
            reason="This trusted extension runs with your host privileges; its effects are not classified by Vikram.",
            url="",
            details=argument_preview(arguments),
        )
        try:
            return bool(
                await asyncio.wait_for(
                    self.interaction.approve(request), APPROVAL_TIMEOUT
                )
            )
        except asyncio.TimeoutError:
            return False

    async def _invoke(
        self, method: str, name: str, arguments: dict, call_id: str
    ) -> dict:
        try:
            self._require_normal()
            if not self._started:
                await self.start()
            package, operation = self._parts(name)
            current = self._read()["packages"].get(package)
            if not current or not current["enabled"]:
                raise ValueError("This extension is disabled or unavailable.")
            worker = self._workers.get(package)
            if worker is None:
                raise ValueError("This extension is disabled or unavailable.")
            if worker.environment != self._path(current):
                raise ValueError(
                    "This extension was updated in another instance. Reload extensions."
                )
            registrations = worker.description.get(
                "tools" if method == "tool" else "commands", []
            )
            spec = next(
                (item for item in registrations if item["name"] == operation), None
            )
            if spec is None:
                raise ValueError("Unknown extension operation.")
            validate_value(arguments, spec["input_schema"])
            if not await self._approve(name, call_id, arguments):
                return {
                    "ok": False,
                    "output": "",
                    "error": "The extension action was not approved.",
                    "activity": "denied",
                    "ui": [],
                }
            result = await worker.request(
                method, {"name": operation, "arguments": arguments}
            )
            if not isinstance(result, dict):
                raise ValueError("Invalid extension result.")
            return {**result, "activity": "complete" if result.get("ok") else "failed"}
        except asyncio.CancelledError:
            if "worker" in locals() and worker is not None:
                await worker.close()
                self._errors[package] = (
                    "The extension operation was stopped. Reload extensions to restart its worker."
                )
            raise
        except Exception:
            if "worker" in locals() and worker is not None and worker.process is None:
                self._errors[package] = (
                    "The extension worker stopped after an error. Reload extensions to restart it."
                )
            return {
                "ok": False,
                "output": "",
                "error": "The extension operation failed or is unavailable. Check extension status.",
                "activity": "failed",
                "ui": [],
            }

    async def call_tool(
        self, name: str, arguments: dict, call_id: str | None = None
    ) -> dict:
        call_id = call_id or uuid4().hex
        if not isinstance(call_id, str) or len(call_id) > 256:
            return {
                "ok": False,
                "output": "",
                "error": "Invalid extension call ID.",
                "activity": "failed",
                "ui": [],
            }
        try:
            signature = hashlib.sha256(
                json.dumps([name, arguments], sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
        except (TypeError, ValueError):
            return {
                "ok": False,
                "output": "",
                "error": "Invalid extension arguments.",
                "activity": "failed",
                "ui": [],
            }
        if call_id in self._calls:
            previous, task = self._calls[call_id]
            if previous != signature or task.cancelled():
                return {
                    "ok": False,
                    "output": "",
                    "error": "This call ID was already used or stopped; it will not be replayed.",
                    "activity": "failed",
                    "ui": [],
                }
            return await task
        if len(self._calls) >= MAX_CALLS:
            return {
                "ok": False,
                "output": "",
                "error": "The extension call budget is exhausted.",
                "activity": "failed",
                "ui": [],
            }
        task = asyncio.create_task(self._invoke("tool", name, arguments, call_id))
        self._calls[call_id] = (signature, task)
        return await task

    async def command(self, name: str, arguments: dict) -> dict:
        return await self._invoke("command", name, arguments, uuid4().hex)

    async def hook(self, event: str, payload: dict) -> list[dict]:
        if self.mode != "normal":
            return []
        if not self._started:
            await self.start()

        def reference(value):
            if isinstance(value, dict):
                return {
                    key: reference(item)
                    for key, item in value.items()
                    if key not in {"reasoning", "reasoning_truncated", "thinking_trace"}
                }
            if isinstance(value, list):
                return [reference(item) for item in value]
            return value

        payload = reference(payload)
        results = []
        records = self._read()["packages"]
        for package, worker in self._workers.items():
            if (
                package not in records
                or not records[package]["enabled"]
                or worker.environment != self._path(records[package])
            ):
                continue
            if event not in worker.description.get("hooks", []):
                continue
            try:
                results.extend(
                    await worker.request(
                        "hook", {"event": event, "payload": payload}, timeout=20
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                results.append(
                    {
                        "ok": False,
                        "output": "",
                        "error": "An extension lifecycle hook failed.",
                        "ui": [],
                    }
                )
        return results
