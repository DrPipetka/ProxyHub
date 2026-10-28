from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psutil

from .adapter import CoreAdapter, CoreError, NoPorts
from .installer import install_xray


def outbound(c, tag):
    p, o = c["protocol"], c["options"]
    server = {"address": c["host"], "port": c["port"]}
    if p in ("vless", "vmess"):
        user = {"id": c["credential"]}
        if p == "vless":
            user.update(encryption=o.get("encryption", "none"))
            if o.get("flow"):
                user["flow"] = o["flow"]
        else:
            user.update(alterId=o.get("alter_id", 0), security=o.get("cipher", "auto"))
        settings = {"vnext": [{**server, "users": [user]}]}
    elif p in ("ss", "trojan"):
        server["password"] = c["credential"]
        if p == "ss":
            server["method"] = o.get("method", "aes-256-gcm")
        settings = {"servers": [server]}
    elif p == "hysteria2":
        settings = {**server, "version": 2}
    else:
        raise CoreError("unsupported_protocol")
    network = o.get("network", "tcp")
    stream = {"network": network, "security": o.get("security", "none")}
    if stream["security"] == "tls":
        stream["tlsSettings"] = {"serverName": o.get("sni", c["host"]), "allowInsecure": o.get("insecure", False)}
        for key, target in (("alpn", "alpn"), ("fingerprint", "fingerprint")):
            if o.get(key):
                stream["tlsSettings"][target] = o[key]
    elif stream["security"] == "reality":
        stream["realitySettings"] = {"serverName": o.get("sni", c["host"]), "fingerprint": o.get("fingerprint", "chrome"),
                                     "publicKey": o.get("public_key", ""), "shortId": o.get("short_id", ""), "spiderX": o.get("spider_x", "")}
    if network in ("ws", "httpupgrade", "xhttp"):
        value = {"path": o.get("path", "/")}
        if o.get("host"):
            if network == "ws":
                value["headers"] = {"Host": o["host"]}
            else:
                value["host"] = o["host"]
        if network == "xhttp":
            value["mode"] = o.get("mode", "auto")
        stream[network + "Settings"] = value
    if network == "grpc":
        stream["grpcSettings"] = {"serviceName": o.get("service_name", ""), "multiMode": o.get("mode") == "multi"}
    if p == "hysteria2":
        stream["network"] = "hysteria"
        stream["hysteriaSettings"] = {"version": 2, "auth": c["credential"]}
        stream.setdefault("tlsSettings", {}).setdefault("alpn", ["h3"])
    return {"tag": tag, "protocol": {"ss": "shadowsocks", "hysteria2": "hysteria"}.get(p, p), "settings": settings, "streamSettings": stream}


def inbound(port, tag, settings=None):
    auth = {"auth": "noauth", "udp": True}
    if settings and settings.socks_auth:
        auth.update(auth="password", accounts=[{"user": settings.socks_username, "pass": settings.socks_password}])
    return {"tag": tag, "listen": settings.bind_address if settings else "127.0.0.1", "port": port, "protocol": "socks", "settings": auth}


def build_config(entries, settings=None, api_port=None):
    # A blocked default prevents accidental direct traffic when a route is absent.
    config = {"log": {"loglevel": "none"}, "inbounds": [], "outbounds": [{"tag": "blocked", "protocol": "blackhole"}],
              "routing": {"domainStrategy": "AsIs", "rules": []}}
    if api_port:
        config["api"] = {"tag": "api", "services": ["HandlerService", "RoutingService"]}
        config["inbounds"].append({"tag": "api-in", "listen": "127.0.0.1", "port": api_port, "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}})
        config["routing"]["rules"].append({"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"})
    for proxy_id, c, port in entries:
        tag = f"p{proxy_id}"
        config["inbounds"].append(inbound(port, tag, settings))
        config["outbounds"].append(outbound(c, tag))
        config["routing"]["rules"].append({"type": "field", "inboundTag": [tag], "outboundTag": tag})
    return config


def port_available(port, host="127.0.0.1"):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        # Both TCP and UDP are used by a SOCKS inbound.
        with socket.socket(family, socket.SOCK_STREAM) as tcp, socket.socket(family, socket.SOCK_DGRAM) as udp:
            tcp.bind((host, port))
            udp.bind((host, port))
        return True
    except OSError:
        return False


class PortAllocator:
    def __init__(self, span, host="127.0.0.1"):
        self.span, self.host, self.used = span, host, set()

    def take(self):
        for port in range(self.span[0], self.span[1] + 1):
            if port not in self.used and port_available(port, self.host):
                self.used.add(port)
                return port
        raise NoPorts("no_free_ports")

    def release(self, port):
        self.used.discard(port)


class XrayAdapter(CoreAdapter):
    def __init__(self, settings, directory, event=lambda *a, **kw: None):
        self.settings, self.directory, self.event = settings, Path(directory), event
        self.directory.mkdir(parents=True, exist_ok=True)
        self.runtime = self.directory / "runtime"
        self.runtime.mkdir(exist_ok=True)
        if os.name != "nt":
            self.runtime.chmod(0o700)
        self.pid_file = self.runtime / "pids.json"
        self.processes = {}
        self.pool = None
        self.entries = {}
        self.api_port = None
        self.lock = asyncio.Lock()
        self.protocols = set(CoreAdapter.protocols)
        self.ready = False
        self.version = "unavailable"
        self.path = None
        self.initialized = False

    def _save_pids(self):
        values = []
        for proc, path in self.processes.values():
            try:
                p = psutil.Process(proc.pid)
                values.append({"pid": p.pid, "created": p.create_time(), "exe": p.exe(), "config": str(path)})
            except psutil.Error:
                pass
        tmp = self.pid_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(values), encoding="utf-8")
        tmp.replace(self.pid_file)

    def cleanup_orphans(self):
        if self.pid_file.exists():
            try:
                records = json.loads(self.pid_file.read_text(encoding="utf-8"))
                for r in records:
                    try:
                        p = psutil.Process(r["pid"])
                        if abs(p.create_time() - r["created"]) < 0.01 and p.exe() == r["exe"] and r["config"] in p.cmdline() and Path(r["config"]).parent == self.runtime:
                            for child in p.children(recursive=True):
                                child.kill()
                            p.kill()
                            p.wait(timeout=5)
                    except (psutil.Error, KeyError):
                        pass
            except (ValueError, OSError):
                self.event("Не удалось прочитать реестр процессов", level="WARNING")
        for p in self.runtime.glob("*.json"):
            if p != self.pid_file:
                p.unlink(missing_ok=True)
        self._save_pids()

    async def _command(self, *args, timeout=15):
        proc = await asyncio.create_subprocess_exec(str(self.path), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            # Never expose xray's raw output: it may echo credentials from configs.
            raise CoreError("xray_command_failed:" + args[0])
        return output.decode(errors="replace")

    def _write(self, config):
        path = self.runtime / (uuid.uuid4().hex + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f)
        return path

    async def validate(self, config):
        path = self._write(config)
        try:
            await self._command("run", "-test", "-config", str(path))
            return True
        except CoreError:
            return False
        finally:
            path.unlink(missing_ok=True)

    async def initialize(self):
        if not self.initialized:
            self.cleanup_orphans()
            self.initialized = True
        elif self.pool is not None:
            await self._stop(self.pool)
        binary_dir = Path("bin").resolve()
        self.path = Path(self.settings.xray_path).resolve() if self.settings.xray_path else binary_dir / ("xray.exe" if os.name == "nt" else "xray")
        if not self.path.is_file():
            if self.settings.xray_path or not self.settings.auto_download:
                raise CoreError("xray_binary_not_found")
            self.path = await install_xray(binary_dir)
        self.version = (await self._command("version")).splitlines()[0][:200]
        probe = {"protocol": "hysteria2", "host": "example.com", "port": 443, "credential": "probe", "options": {"security": "tls", "network": "hysteria"}}
        if await self.validate(build_config([(0, probe, 20000)])):
            self.protocols.add("hysteria2")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.api_port = s.getsockname()[1]
        self.pool = await self._start(build_config([], self.settings, self.api_port), [("127.0.0.1", self.api_port)])
        self.ready = True

    async def _start(self, config, endpoints):
        path = self._write(config)
        try:
            proc = await asyncio.create_subprocess_exec(str(self.path), "run", "-config", str(path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.processes[proc.pid] = (proc, path)
        self._save_pids()
        try:
            for host, port in endpoints:
                await self._wait_port(proc, host, port)
        except BaseException:
            await self._stop(proc)
            raise
        return proc

    async def _wait_port(self, proc, host, port):
        host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if proc.returncode is not None:
                raise CoreError("xray_process_exited")
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 0.3)
                writer.close()
                await writer.wait_closed()
                return
            except (OSError, TimeoutError):
                await asyncio.sleep(0.05)
        raise CoreError("xray_start_timeout")

    async def _stop(self, proc):
        if proc is None:
            return
        if proc.returncode is None:
            try:
                p = psutil.Process(proc.pid)
                for child in p.children(recursive=True):
                    child.kill()
                proc.terminate()
            except (psutil.Error, ProcessLookupError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        entry = self.processes.pop(proc.pid, None)
        if entry:
            entry[1].unlink(missing_ok=True)
        self._save_pids()

    @asynccontextmanager
    async def batch(self, entries):
        proc = await self._start(build_config(entries), [("127.0.0.1", p) for _, _, p in entries])
        try:
            yield
        finally:
            await self._stop(proc)

    async def _api(self, command, data=None, tag=None):
        path = self._write(data) if data is not None else None
        try:
            await self._command("api", command, f"--server=127.0.0.1:{self.api_port}", "--timeout=2", str(path) if path else tag, timeout=4)
        finally:
            if path:
                path.unlink(missing_ok=True)

    async def _restart_pool(self):
        await self._stop(self.pool)
        self.pool = await self._start(build_config(list(self.entries.values()), self.settings, self.api_port), [("127.0.0.1", self.api_port)])
        self.event("Пул xray перезапущен; существующие соединения прерваны", level="WARNING")

    async def add(self, proxy_id, config, port):
        async with self.lock:
            entry = (proxy_id, config, port)
            self.entries[proxy_id] = entry
            try:
                delta = build_config([entry], self.settings)
                await self._api("ado", {"outbounds": delta["outbounds"][1:]})
                await self._api("adrules", {"routing": build_config(list(self.entries.values()), self.settings, self.api_port)["routing"]})
                await self._api("adi", {"inbounds": delta["inbounds"]})
                await self._wait_port(self.pool, self.settings.bind_address, port)
            except CoreError:
                if self.settings.pool_restart_fallback:
                    await self._restart_pool()
                else:
                    self.entries.pop(proxy_id, None)
                    for cmd in ("rmi", "rmo"):
                        try:
                            await self._api(cmd, tag=f"p{proxy_id}")
                        except CoreError:
                            pass
                    raise

    async def remove(self, proxy_id):
        async with self.lock:
            if proxy_id not in self.entries:
                return
            try:
                await self._api("rmi", tag=f"p{proxy_id}")
                await self._api("rmo", tag=f"p{proxy_id}")
                self.entries.pop(proxy_id, None)
                await self._api("adrules", {"routing": build_config(list(self.entries.values()), self.settings, self.api_port)["routing"]})
            except CoreError:
                self.entries.pop(proxy_id, None)
                # Fail closed: an untracked inbound must never remain reachable.
                self.event("Ошибка удаления через API; аварийное закрытие процесса для освобождения порта", proxy_id, "ERROR")
                await self._restart_pool()

    async def close(self):
        self.ready = False
        for proc, _ in list(self.processes.values()):
            await self._stop(proc)
        self.entries.clear()
