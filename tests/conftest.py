import asyncio
import os
from contextlib import asynccontextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import websockets
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def ws_server_factory():
    """Start an in-process websockets server on a random localhost port."""

    @asynccontextmanager
    async def start(handler):
        server = await websockets.serve(handler, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            yield port
        finally:
            server.close()
            await server.wait_closed()

    return start
