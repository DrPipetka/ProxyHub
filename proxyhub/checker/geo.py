import asyncio
import gzip
from datetime import datetime, timezone
from pathlib import Path

import httpx
import maxminddb

from ..core.installer import download_limited


class GeoDatabase:
    def __init__(self):
        self.reader = None

    async def initialize(self, settings, directory):
        path = Path(settings.mmdb_path) if settings.mmdb_path else Path(directory) / "dbip-country-lite.mmdb"
        if not path.exists() and settings.auto_download and not settings.mmdb_path:
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            async with httpx.AsyncClient(timeout=120, follow_redirects=True, trust_env=False) as client:
                data = await download_limited(client, f"https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz", 30 * 1024 * 1024)
            tmp = path.with_suffix(".part")
            await asyncio.to_thread(tmp.write_bytes, gzip.decompress(data))
            # Validate before replacing the database.
            with maxminddb.open_database(str(tmp)):
                pass
            tmp.replace(path)
        self.reader = maxminddb.open_database(str(path))

    def country(self, ip):
        if self.reader is None:
            raise RuntimeError("geo_database_unavailable")
        result = self.reader.get(ip) or {}
        return result.get("country", {}).get("iso_code")

    def close(self):
        if self.reader:
            self.reader.close()
