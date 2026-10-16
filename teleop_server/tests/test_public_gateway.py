import asyncio
import tempfile
import unittest

from aiohttp import ClientSession, web

from teleop_server.public_gateway import Accounts, COOKIE, create_app


class PublicGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = Accounts(self.tmp.name)
        self.accounts.add("alice", "long-test-password-123")

        async def echo(request):
            if request.path == "/ws":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for message in ws:
                    await ws.send_str(message.data)
                return ws
            return web.json_response({"path": request.path, "method": request.method})

        self.backend = web.Application()
        self.backend.router.add_route("*", "/{tail:.*}", echo)
        self.backend_runner = web.AppRunner(self.backend)
        await self.backend_runner.setup()
        backend_site = web.TCPSite(self.backend_runner, "127.0.0.1", 0)
        await backend_site.start()
        backend_port = backend_site._server.sockets[0].getsockname()[1]
        self.gateway = create_app(self.accounts, f"http://127.0.0.1:{backend_port}")
        self.runner = web.AppRunner(self.gateway)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.client = ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()
        await self.backend_runner.cleanup()
        self.tmp.cleanup()

    async def test_authentication_blocks_control_and_private_machine_channels(self):
        async with self.client.post(self.url + "/api/control", json={}) as response:
            self.assertEqual(response.status, 401)
        async with self.client.post(self.url + "/login", data={"username": "alice", "password": "wrong"},
                                    allow_redirects=False) as response:
            self.assertEqual(response.status, 200)
        async with self.client.post(self.url + "/login", data={"username": "alice", "password": "long-test-password-123"},
                                    allow_redirects=False) as response:
            self.assertEqual(response.status, 303)
            token = response.cookies[COOKIE].value
            self.assertTrue(response.cookies[COOKIE]["secure"])
        headers = {"Cookie": f"{COOKIE}={token}"}
        async with self.client.post(self.url + "/api/control", headers=headers, json={}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["path"], "/api/control")
        for path in ("/edge/control", "/policy/ws", "/api/policy"):
            async with self.client.get(self.url + path, headers=headers) as response:
                self.assertEqual(response.status, 404)
        self.accounts.remove("alice")
        async with self.client.post(self.url + "/api/control", headers=headers, json={}) as response:
            self.assertEqual(response.status, 401)

    async def test_authenticated_websocket_proxies_both_directions(self):
        token = self.accounts.issue("alice")
        async with self.client.ws_connect(self.url.replace("http", "ws") + "/ws",
                                          headers={"Cookie": f"{COOKIE}={token}"}) as ws:
            await ws.send_str("camera-ready")
            self.assertEqual((await asyncio.wait_for(ws.receive(), 2)).data, "camera-ready")


if __name__ == "__main__":
    unittest.main()
