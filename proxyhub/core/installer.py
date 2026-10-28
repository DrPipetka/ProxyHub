from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import zipfile
from pathlib import Path

import httpx


def asset_name(system=None, machine=None):
    system, machine = system or platform.system(), (machine or platform.machine()).lower()
    arch = {"amd64": "64", "x86_64": "64", "arm64": "arm64-v8a", "aarch64": "arm64-v8a"}.get(machine)
    if system not in {"Windows", "Linux"} or arch is None:
        raise RuntimeError("Автозагрузка поддерживает Windows/Linux x64/arm64")
    if system == "Windows" and arch == "arm64-v8a":
        arch = "arm64-v8a"
    return f"Xray-{system.lower()}-{arch}.zip"


def verify_digest(data: bytes, digest_text: str):
    hashes = re.findall(r"(?i)\b[0-9a-f]{64}\b", digest_text)
    expected = hashlib.sha256(data).hexdigest()
    if expected not in [h.lower() for h in hashes]:
        raise RuntimeError("SHA256 сборки xray не совпадает")


async def download_limited(client, url, limit):
    result = bytearray()
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            result.extend(chunk)
            if len(result) > limit:
                raise ValueError("Загружаемый файл превышает лимит")
    return bytes(result)


async def install_xray(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("xray.exe" if os.name == "nt" else "xray")
    if target.exists():
        return target.resolve()
    name = asset_name()
    async with httpx.AsyncClient(timeout=120, follow_redirects=True, trust_env=False) as client:
        response = await client.get("https://api.github.com/repos/XTLS/Xray-core/releases/latest")
        response.raise_for_status()
        release = response.json()
        assets = {a["name"]: a["browser_download_url"] for a in release["assets"]}
        if name not in assets or name + ".dgst" not in assets:
            raise RuntimeError("В релизе нет сборки или контрольной суммы для этой платформы")
        archive = await download_limited(client, assets[name], 150 * 1024 * 1024)
        digest = (await download_limited(client, assets[name + ".dgst"], 65536)).decode()
    verify_digest(archive, digest)
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        entry = next(i for i in z.infolist() if Path(i.filename).name == target.name)
        if entry.file_size > 200 * 1024 * 1024:
            raise RuntimeError("Слишком большой исполняемый файл")
        tmp = target.with_suffix(".part")
        tmp.write_bytes(z.read(entry))
        if os.name != "nt":
            tmp.chmod(0o700)
        tmp.replace(target)
    (directory / "release.txt").write_text(release["tag_name"] + "\nSHA256 " + hashlib.sha256(archive).hexdigest(), encoding="utf-8")
    return target.resolve()
