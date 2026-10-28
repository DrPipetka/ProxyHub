"""Download the verified Xray release without starting the application."""
import asyncio
from pathlib import Path

from proxyhub.core.installer import install_xray


if __name__ == "__main__":
    print(asyncio.run(install_xray(Path("bin"))))
