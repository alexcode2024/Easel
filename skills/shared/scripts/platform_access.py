"""Shared platform-neutral account access contracts and runtime primitives."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import random
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
RESOURCE_ENV = "EASEL_PLATFORM_ACCESS_RESOURCE"


class AccessStopped(RuntimeError):
    pass


class RequestBudgetExhausted(AccessStopped):
    pass


@dataclass(frozen=True)
class AccessResource:
    platform: str
    account_id: str = "default"

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.account_id}"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class AccessPolicy:
    platform: str
    display_name: str
    concurrency: int = 1
    request_delay_min: float = 0
    request_delay_max: float = 0
    risk_cooldown: int = 0
    request_budget: int = 20
    command_timeout: int = 90

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("platform concurrency must be at least one")
        if self.request_delay_min < 0 or self.request_delay_max < self.request_delay_min:
            raise ValueError("request delay range must be non-negative and ordered")
        if self.risk_cooldown < 0 or self.request_budget < 0 or self.command_timeout < 1:
            raise ValueError("risk cooldown and request budget must be non-negative and timeout positive")


@dataclass(frozen=True)
class AccessResult:
    exit_code: int = 0
    payload: dict | None = None
    text: str = ""


@dataclass
class PlatformPolicyAdapter:
    policy: AccessPolicy
    domains: tuple[str, ...]
    api_paths: tuple[str, ...]
    risk_pattern: re.Pattern
    persistent_path: Path
    shared_lock_path: Path | None = None
    shared_pace_path: Path | None = None
    risk_codes: tuple[str, ...] = ("429", "461")
    risk_exit_codes: tuple[int, ...] = (2, 429, 461)
    risk_fields: tuple[str, ...] = ("risk", "risk_control")
    response_text_pattern: re.Pattern | None = None

    def state_path(self, resource: AccessResource) -> Path:
        if resource.account_id == "default":
            return self.persistent_path
        return self.persistent_path.parent / "platform_access" / f"{resource.digest}.json"

    def check_breaker(self, resource: AccessResource) -> None:
        state = _read_json(self.state_path(resource))
        if float(state.get("risk_until") or 0) > time.time():
            raise AccessStopped(state.get("risk_reason") or
                                f"{self.policy.display_name} 风控暂停中，本轮停止且不自动重试")

    def trip(self, resource: AccessResource, reason: str) -> dict:
        now = int(time.time())
        state = _read_json(self.state_path(resource))
        until = int(state.get("risk_until") or 0)
        if until <= now:
            until = now + self.policy.risk_cooldown
            state.update(risk_until=until, risk_reason=str(reason)[:240], risk_triggered_at=now)
            _write_json(self.state_path(resource), state)
        return self.risk_status(resource)

    def risk_status(self, resource: AccessResource) -> dict:
        data = _read_json(self.state_path(resource))
        until = int(data.get("risk_until") or 0)
        active = until > int(time.time())
        return {"active": active, "until": until if active else 0,
                "reason": str(data.get("risk_reason") or "") if active else "",
                "triggered_at": int(data.get("risk_triggered_at") or 0)}

    def inspect_status(self, response, resource: AccessResource) -> None:
        status = getattr(response, "status_code", getattr(response, "status", None))
        if status is not None and str(status) in self.risk_codes:
            self.trip(resource, f"HTTP {status}")
            raise AccessStopped(f"HTTP {status}")

    def inspect_response(self, response, resource: AccessResource) -> None:
        self.inspect_status(response, resource)
        text = getattr(response, "text", "")
        try:
            text = text() if callable(text) else text
        except Exception:
            text = ""
        structured = False
        try:
            body = json.loads(text)
            if isinstance(body, dict):
                structured = True
                code = str(body.get("code") or body.get("status_code") or "")
                if code in self.risk_codes:
                    self.trip(resource, f"平台错误码 {code}")
                    raise AccessStopped(f"平台错误码 {code}")
                text = " ".join(str(body.get(key) or "") for key in
                                ("code", "status_code", "msg", "message", "error", "risk_control"))
        except (ValueError, TypeError):
            pass
        pattern = self.risk_pattern if structured else (self.response_text_pattern or self.risk_pattern)
        marker = pattern.search(str(text))
        if marker:
            reason = f"平台校验信号 {marker.group()}"
            self.trip(resource, reason)
            raise AccessStopped(reason)

    def classify_result(self, result: AccessResult) -> str:
        payload = result.payload or {}
        details = " ".join(str(payload.get(key) or "") for key in
                            ("error", "message", "msg", "code", "status_code", *self.risk_fields))
        marker = self.risk_pattern.search(f"{details} {result.text or ''}")
        if marker:
            return f"平台校验信号：{marker.group()}"
        if result.exit_code in self.risk_exit_codes or any(payload.get(key) for key in self.risk_fields):
            return f"{self.policy.display_name}访问限制，本轮已停止"
        return ""

    def mark_result(self, resource: AccessResource, result: AccessResult) -> str:
        reason = self.classify_result(result)
        if not reason:
            return ""
        self.trip(resource, reason)
        return reason

    def matches_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not any(host == domain or host.endswith("." + domain) for domain in self.domains):
            return False
        return any(parsed.path.startswith(path) for path in self.api_paths)

    def lock_path(self, resource: AccessResource, slot: int = 0) -> Path:
        base = self.shared_lock_path or default_lock_path(resource)
        if resource.account_id != "default" and self.shared_lock_path:
            base = base.with_name(f"{base.stem}-{resource.digest}{base.suffix}")
        if slot == 0:
            return base
        return base.with_name(f"{base.stem}.slot-{slot}{base.suffix}")

    def pace_path(self, resource: AccessResource) -> Path:
        if resource.account_id != "default" and self.shared_pace_path:
            base = self.shared_pace_path
            return base.with_name(f"{base.stem}-{resource.digest}{base.suffix}")
        return self.shared_pace_path or default_pace_path(resource)


class PlatformAdapter(Protocol):
    policy: AccessPolicy

    def check_breaker(self, resource: AccessResource) -> None: ...
    def trip(self, resource: AccessResource, reason: str) -> dict: ...
    def risk_status(self, resource: AccessResource) -> dict: ...
    def state_path(self, resource: AccessResource) -> Path: ...
    def inspect_response(self, response, resource: AccessResource) -> None: ...
    def inspect_status(self, response, resource: AccessResource) -> None: ...
    def classify_result(self, result: AccessResult) -> str: ...
    def mark_result(self, resource: AccessResource, result: AccessResult) -> str: ...
    def matches_url(self, url: str) -> bool: ...
    def lock_path(self, resource: AccessResource, slot: int = 0) -> Path: ...
    def pace_path(self, resource: AccessResource) -> Path: ...


_ADAPTERS: dict[str, PlatformAdapter] = {}


def register_adapter(adapter: PlatformAdapter) -> PlatformAdapter:
    platform = adapter.policy.platform
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", platform):
        raise ValueError(f"invalid platform key: {platform}")
    _ADAPTERS[platform] = adapter
    return adapter


def cli_option(argv: list[str], name: str) -> str | None:
    prefix = f"{name}="
    for index, argument in enumerate(argv):
        if argument == name and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith(prefix):
            return argument[len(prefix):]
    return None


def get_adapter(platform: str) -> PlatformAdapter | None:
    return _ADAPTERS.get(platform)


def classify_error(adapter: PlatformAdapter | str, text: str) -> str:
    if isinstance(adapter, str):
        registered = get_adapter(adapter)
        if registered is None:
            return ""
        adapter = registered
    return adapter.classify_result(AccessResult(text=str(text)))


def adapters() -> tuple[PlatformAdapter, ...]:
    return tuple(_ADAPTERS[key] for key in sorted(_ADAPTERS))


def default_lock_path(resource: AccessResource) -> Path:
    return ROOT / "outputs" / "_locks" / "platform-access" / f"{resource.digest}.lock"


def default_pace_path(resource: AccessResource) -> Path:
    return ROOT / "outputs" / "_locks" / "platform-access" / f"{resource.digest}.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class FileResourceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None
        self.acquired = False

    def acquire(self, timeout: float | None = None, poll: float = 0.2) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
        else:
            import fcntl
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return True
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                if timeout == 0:
                    return False
                time.sleep(poll)

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if self.acquired:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
            self.acquired = False


@dataclass
class _ActiveSession:
    adapter: PlatformAdapter
    resource: AccessResource
    lock: FileResourceLock | None
    budget: int
    requests: int = 0
    depth: int = 1


_ACTIVE_SESSION: contextvars.ContextVar[_ActiveSession | None] = contextvars.ContextVar(
    "platform_access_session", default=None)


def is_active(platform: str | None = None) -> bool:
    active = _ACTIVE_SESSION.get()
    return bool(active and (platform is None or active.resource.platform == platform))


def active_resource() -> AccessResource | None:
    active = _ACTIVE_SESSION.get()
    return active.resource if active else None


def environment_for(resource: AccessResource, env: dict[str, str] | None = None) -> dict[str, str]:
    child_env = dict(os.environ if env is None else env)
    child_env[RESOURCE_ENV] = resource.key
    return child_env


def _acquire_resource_lock(adapter: PlatformAdapter, resource: AccessResource) -> FileResourceLock:
    while True:
        for slot in range(adapter.policy.concurrency):
            lock = FileResourceLock(adapter.lock_path(resource, slot))
            if lock.acquire(timeout=0):
                return lock
            if lock.handle is None:
                raise RuntimeError("无法创建平台访问锁，已拒绝访问")
            lock.release()
        time.sleep(0.2)


@contextmanager
def session(adapter: PlatformAdapter | str, *, account_id: str = "default",
            budget: int | None = None):
    if isinstance(adapter, str):
        registered = get_adapter(adapter)
        if registered is None:
            raise ValueError(f"no access adapter registered for {adapter}")
        adapter = registered
    resource = AccessResource(adapter.policy.platform, account_id)
    active = _ACTIVE_SESSION.get()
    if active:
        if active.resource != resource:
            raise RuntimeError("nested access session must use the same platform account")
        active.depth += 1
        try:
            yield active
        finally:
            active.depth -= 1
        return

    adapter.check_breaker(resource)
    inherited = os.environ.get(RESOURCE_ENV) == resource.key
    lock = None if inherited else _acquire_resource_lock(adapter, resource)
    state = _ActiveSession(adapter, resource, lock,
                           max(0, int(budget if budget is not None else adapter.policy.request_budget)))
    token = _ACTIVE_SESSION.set(state)
    previous_resource = os.environ.get(RESOURCE_ENV)
    os.environ[RESOURCE_ENV] = resource.key
    try:
        adapter.check_breaker(resource)
        yield state
    finally:
        if previous_resource is None:
            os.environ.pop(RESOURCE_ENV, None)
        else:
            os.environ[RESOURCE_ENV] = previous_resource
        _ACTIVE_SESSION.reset(token)
        if lock:
            lock.release()


def set_request_budget(budget: int) -> None:
    active = _ACTIVE_SESSION.get()
    if active:
        active.budget = max(0, int(budget))


def request(call, *args, **kwargs):
    active = _ACTIVE_SESSION.get()
    if active is None:
        return call(*args, **kwargs)
    active.adapter.check_breaker(active.resource)
    if active.requests >= active.budget:
        raise RequestBudgetExhausted("本轮请求预算已用完，停止访问")
    minimum = active.adapter.policy.request_delay_min
    maximum = active.adapter.policy.request_delay_max
    pace_lock = None
    try:
        if maximum > 0:
            pace_path = active.adapter.pace_path(active.resource)
            pace_lock = FileResourceLock(pace_path.with_name(f"{pace_path.name}.lock"))
            pace_lock.acquire()
            finished_at = float(_read_json(pace_path).get("finished_at") or time.time())
            time.sleep(max(0, finished_at + random.uniform(minimum, maximum) - time.time()))
            active.adapter.check_breaker(active.resource)
        active.requests += 1
        try:
            response = call(*args, **kwargs)
            active.adapter.inspect_response(response, active.resource)
            return response
        finally:
            if maximum > 0:
                _write_json(active.adapter.pace_path(active.resource), {"finished_at": time.time()})
    finally:
        if pace_lock:
            pace_lock.release()


def run_cli(main, *, platform: str | PlatformAdapter, enabled: bool = True,
            account_id: str = "default", budget: int | None = None) -> int:
    import sys

    offline = len(sys.argv) > 1 and sys.argv[1] in ("selftest", "check", "plan")
    if not enabled or offline or "--help" in sys.argv or "-h" in sys.argv:
        return main()
    try:
        with session(platform, account_id=account_id, budget=budget):
            return main()
    except RequestBudgetExhausted as error:
        print(json.dumps({"error": str(error), "budget_exhausted": True}, ensure_ascii=False))
        return 3
    except AccessStopped as error:
        print(json.dumps({"error": str(error), "risk": str(error)}, ensure_ascii=False))
        return 2


def install_browser_guard(context, *, platform: str | None = None) -> None:
    active = _ACTIVE_SESSION.get()
    if active is None or (platform and active.resource.platform != platform):
        return
    adapter = active.adapter

    def gate(route):
        if getattr(route.request, "resource_type", "") not in ("xhr", "fetch"):
            route.continue_()
            return
        try:
            response = request(route.fetch, max_redirects=0)
            route.fulfill(response=response)
        except AccessStopped:
            route.abort()
            context.close()

    context.route(adapter.matches_url, gate)

    def inspect(response):
        if getattr(response.request, "resource_type", "") not in ("xhr", "fetch"):
            return
        if not adapter.matches_url(response.url):
            return
        try:
            adapter.check_breaker(active.resource)
            adapter.inspect_response(response, active.resource)
        except AccessStopped:
            context.close()

    context.on("response", inspect)


class AsyncResourceQueue:
    def __init__(self, poll_interval: float = 0.2):
        self.poll_interval = poll_interval
        self._queues: dict[str, list[dict]] = {}

    def queue(self, resource: AccessResource) -> list[dict]:
        return self._queues.setdefault(resource.key, [])

    async def enter(self, adapter: PlatformAdapter, resource: AccessResource, label: str,
                    risk_check, *, use_process_lock: bool = True) -> dict:
        queue = self.queue(resource)
        entry = {"id": hashlib.sha256(f"{time.time_ns()}:{os.getpid()}".encode()).hexdigest()[:12],
                 "platform": resource.platform, "resource_key": resource.key,
                 "label": label, "state": "queued", "queued_at": int(time.time()),
                 "started_at": 0, "message": ""}
        queue.append(entry)
        lock = None
        try:
            while True:
                risk_check(resource)
                earlier = queue[:queue.index(entry)] if entry in queue else []
                earlier_active = sum(item.get("state") == "running" for item in earlier)
                earlier_waiting = sum(item.get("state") == "queued" for item in earlier)
                active = sum(item.get("state") == "running" for item in queue)
                if active < adapter.policy.concurrency and earlier_active + earlier_waiting < adapter.policy.concurrency:
                    if not use_process_lock:
                        risk_check(resource)
                        entry.update(state="running", started_at=int(time.time()),
                                     message=f"正在访问{adapter.policy.display_name}")
                        return entry
                    for slot in range(adapter.policy.concurrency):
                        lock = FileResourceLock(adapter.lock_path(resource, slot))
                        if lock.acquire(timeout=0):
                            risk_check(resource)
                            entry.update(state="running", started_at=int(time.time()),
                                         message=f"正在访问{adapter.policy.display_name}",
                                         proc_lock=lock)
                            return entry
                        if lock.handle is None:
                            raise RuntimeError("无法创建平台访问锁，已拒绝访问")
                        lock.release()
                await asyncio.sleep(self.poll_interval)
        except BaseException:
            if lock:
                lock.release()
            if entry in queue:
                queue.remove(entry)
            if not queue:
                self._queues.pop(resource.key, None)
            raise

    def leave(self, entry: dict, state: str = "done", message: str = "") -> None:
        entry.update(state=state, message=message, finished_at=int(time.time()))
        lock = entry.pop("proc_lock", None)
        if lock:
            lock.release()
        queue = self._queues.get(entry.get("resource_key"), [])
        if entry in queue:
            queue.remove(entry)
        if not queue:
            self._queues.pop(entry.get("resource_key"), None)

    def snapshot(self, adapter: PlatformAdapter, resource: AccessResource, risk: dict) -> dict:
        queue = self._queues.get(resource.key, [])
        active = next((entry for entry in queue if entry.get("state") == "running"), None)
        waiting = [{"id": entry["id"], "platform": resource.platform,
                    "label": entry["label"], "state": "queued", "position": index,
                    "queued_at": entry["queued_at"]}
                   for index, entry in enumerate(
                       (entry for entry in queue if entry.get("state") == "queued"), 1)]
        active_count = sum(entry.get("state") == "running" for entry in queue)
        busy_slots = 0
        for slot in range(adapter.policy.concurrency):
            process_lock = FileResourceLock(adapter.lock_path(resource, slot))
            if process_lock.acquire(timeout=0):
                process_lock.release()
            else:
                busy_slots += 1
        external_busy = busy_slots > active_count
        return {"resource": resource.key, "platform": resource.platform,
                "platform_name": adapter.policy.display_name,
                "concurrency": adapter.policy.concurrency,
                "active": ({"id": active["id"], "label": active["label"],
                            "started_at": active["started_at"]} if active else
                           ({"id": "external", "label": "平台访问进程", "started_at": 0}
                            if external_busy else None)),
                "external_busy": external_busy,
                "queue": waiting, "queued": len(waiting), "risk": risk}
