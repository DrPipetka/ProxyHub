from abc import ABC, abstractmethod


class CoreError(RuntimeError):
    pass


class NoPorts(CoreError):
    pass


class CoreAdapter(ABC):
    ready = False
    version = "unavailable"
    protocols = {"vless", "vmess", "ss", "trojan"}

    @abstractmethod
    async def initialize(self): ...

    @abstractmethod
    def batch(self, entries): ...

    @abstractmethod
    async def add(self, proxy_id, config, port): ...

    @abstractmethod
    async def replace(self, proxy_id, config, port): ...

    @abstractmethod
    async def remove(self, proxy_id): ...

    @abstractmethod
    async def close(self): ...
