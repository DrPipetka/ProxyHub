import os

import uvicorn

from .config import data_dir
from .db import Database
from .main import create_app


def main():
    directory = data_dir()
    db = Database(directory / "proxyhub.db")
    settings = db.settings()
    db.close()
    if os.name != "nt":
        import resource
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            print(f"Внимание: лимит файловых дескрипторов {soft}; рекомендуется ulimit -n 4096")
    print(f"ProxyHub: http://{settings.web_host}:{settings.web_port}")
    print("API-токен доступен в разделе «Настройки» после входа.")
    uvicorn.run(create_app(directory), host=settings.web_host, port=settings.web_port, access_log=False, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
