"""The app starting, restarting and stopping the model server, with mlx_server faked."""

import threading
import unittest
from unittest import mock

from textual.widgets import Input

import mlx_server
from app import DecartesApp
from chat import Chat, Spinner
from config import config


class ServerUiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, name, value in [(mlx_server, "ensure_chat", mock.Mock()), (mlx_server, "restart_chat", mock.Mock()),
                                    (mlx_server, "stop_all", mock.Mock(return_value=["chat"])),
                                    (config, "model", "some/model")]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def settle(self, app, pilot) -> None:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_startup_shows_the_spinner_until_the_server_is_ready(self):
        release = threading.Event()
        mlx_server.ensure_chat.side_effect = lambda: release.wait(5)
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.pause(0.05)
            chat = app.query_one(Chat)
            self.assertTrue(chat.busy)
            self.assertEqual(app.query_one(Spinner).label, "Starting model server...")
            release.set()
            await self.settle(app, pilot)
            self.assertFalse(chat.busy)
            self.assertFalse(app.query_one(Input).disabled)

    async def test_failed_start_is_shown_and_the_app_stays_usable(self):
        mlx_server.ensure_chat.side_effect = mlx_server.ModelServerError("no server")
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            self.assertEqual([n.message for n in app._notifications], ["no server"])
            self.assertFalse(app.query_one(Chat).busy)

    async def test_choosing_another_model_restarts_the_server(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.set_model("other/model")
            await self.settle(app, pilot)
            self.assertEqual(config.model, "other/model")
            mlx_server.restart_chat.assert_called_once_with()

    async def test_choosing_the_same_model_does_nothing(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.set_model("some/model")
            await self.settle(app, pilot)
            mlx_server.restart_chat.assert_not_called()

    async def test_model_stays_while_a_reply_is_running(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.query_one(Chat).set_busy(True)
            app.set_model("other/model")
            await pilot.pause()
            self.assertEqual(config.model, "some/model")
            mlx_server.restart_chat.assert_not_called()

    async def test_kill_stops_the_servers(self):
        app = DecartesApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(app, pilot)
            app.query_one(Chat).run_command("kill")
            await self.settle(app, pilot)
            mlx_server.stop_all.assert_called_once_with()
            self.assertEqual([n.message for n in app._notifications], ["Stopped the chat server"])

if __name__ == "__main__":
    unittest.main()
