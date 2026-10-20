"""Local hosts only. Web ChatGPT must connect to the authenticated HTTPS endpoint."""
import os
import asyncio
from .cli import load_config
from .server import build_server

def main():
    os.umask(0o077)
    app,bridge,_=build_server(load_config(),with_worker=False)
    async def run():
        worker=asyncio.create_task(bridge.worker())
        try: await app.bridge_fastmcp.run_stdio_async()
        finally:
            worker.cancel()
            try: await worker
            except asyncio.CancelledError: pass
    asyncio.run(run())
if __name__=='__main__':main()
