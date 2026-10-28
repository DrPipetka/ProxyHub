"""Local development preview with a graceful stop file, useful on Windows."""
import asyncio
from pathlib import Path

import uvicorn

from proxyhub.config import data_dir
from proxyhub.db import Database
from proxyhub.main import create_app


async def main():
    directory = data_dir()
    db = Database(directory / 'proxyhub.db')
    settings = db.settings()
    db.close()
    stop = Path('.tmp/preview.stop')
    stop.parent.mkdir(exist_ok=True)
    stop.unlink(missing_ok=True)
    server = uvicorn.Server(uvicorn.Config(create_app(directory), host=settings.web_host, port=settings.web_port, access_log=False))
    async def watch():
        while not stop.exists() and not server.should_exit:
            await asyncio.sleep(0.25)
        server.should_exit = True
    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
