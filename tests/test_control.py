import asyncio
import importlib
import json
import runpy
import threading
import unittest
from unittest.mock import Mock, patch

from starlette.testclient import TestClient


# 导入不得连接真实设备、授权绑定或启动后台线程。
with patch("threading.Thread.start") as import_start, patch("requests.post") as import_post:
    main = importlib.import_module("main")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.remote = main.KisstoyRemote("test-device", "test-group", "test-share")
        self.remote.is_connected = True
        self.frames = []
        self.remote.ws = Mock()
        self.remote.ws.send.side_effect = lambda payload: self.frames.append(json.loads(payload))
        self.remote_patch = patch.object(main, "remote", self.remote)
        self.remote_patch.start()
        self.addCleanup(self.remote_patch.stop)
        with main._CONTROL_CONDITION:
            main._set_auto_mode(False)

    def motors(self):
        return [frame["data"]["motors"] for frame in self.frames]

    def enable(self):
        main.set_auto_pilot(True)
        return main._AUTO_GENERATION

    def test_import_has_no_device_side_effects(self):
        import_start.assert_not_called()
        import_post.assert_not_called()

    def test_stop_each_motor_leaves_other_motor_running(self):
        for motor, other in ((1, 3), (3, 1)):
            with self.subTest(motor=motor):
                self.frames.clear()
                main.control_device(other, 60)
                main.control_device(motor, 40)
                main.control_device(motor, 0)
                # 按局部通道更新模拟接收端，不应向另一通道发送归零。
                state = {"1": 0, "3": 0}
                for update in self.motors():
                    state.update(update)
                self.assertEqual(state, {str(motor): 0, str(other): 60})
                self.assertEqual(self.motors()[-1], {str(motor): 0})

    def test_stop_all_sends_one_frame_with_both_zeros(self):
        self.enable()
        response = main.stop_all()
        self.assertFalse(main.AUTO_MODE)
        self.assertEqual(self.motors(), [{"1": 0, "3": 0}])
        self.assertEqual(self.frames[0]["event"], "control")
        self.assertEqual(self.frames[0]["data"]["target"], "test-group")
        self.assertEqual(self.frames[0]["data"]["device_id"], "test-device")
        self.assertIn("已发送", response)

    def test_disable_auto_uses_same_stop_all_command(self):
        self.enable()
        main.set_auto_pilot(False)
        self.assertFalse(main.AUTO_MODE)
        self.assertEqual(self.motors(), [{"1": 0, "3": 0}])

    def test_manual_control_cancels_old_auto_steps(self):
        generation = self.enable()
        main.control_device(1, 0)
        self.assertFalse(main._run_auto_step(generation, {"1": 40, "3": 0}, 0))
        self.assertFalse(main.AUTO_MODE)
        self.assertEqual(self.motors(), [{"1": 0}])

    def test_rapid_restart_does_not_revive_previous_auto_cycle(self):
        generation = self.enable()
        main.stop_all()
        self.enable()
        self.assertFalse(main._run_auto_step(generation, {"1": 50, "3": 50}, 0))
        self.assertEqual(self.motors(), [{"1": 0, "3": 0}])

    def test_auto_phases_send_complete_motor_pairs(self):
        generation = self.enable()
        for motors, _ in main.AUTO_STEPS:
            self.assertTrue(main._run_auto_step(generation, motors, 0))
        self.assertEqual(self.motors(), [
            {"1": 40, "3": 0}, {"1": 0, "3": 60},
            {"1": 50, "3": 50}, {"1": 0, "3": 0},
        ])

    def test_invalid_arguments_never_send_or_cancel_auto(self):
        self.enable()
        for motor, intensity in ((0, 0), (2, 50), (3, -1), (1, 101)):
            with self.subTest(motor=motor, intensity=intensity):
                self.assertIn("参数错误", main.control_device(motor, intensity))
        self.assertTrue(main.AUTO_MODE)
        self.assertEqual(self.frames, [])

    def test_offline_commands_report_failure(self):
        self.remote.is_connected = False
        for command in (lambda: main.control_device(1, 0), main.stop_all,
                        lambda: main.set_auto_pilot(False)):
            with self.subTest(command=command):
                self.assertIn("发送失败", command())
        self.assertIn("未开启", main.set_auto_pilot(True))
        self.assertFalse(main.AUTO_MODE)
        self.assertEqual(self.frames, [])

    def test_send_exception_is_not_reported_as_success(self):
        self.remote.ws.send.side_effect = OSError("test disconnect")
        for command in (lambda: main.control_device(3, 0), main.stop_all,
                        lambda: main.set_auto_pilot(False)):
            with self.subTest(command=command):
                self.assertIn("发送失败", command())
        self.assertFalse(main.AUTO_MODE)

    def test_failed_auto_send_cancels_auto(self):
        generation = self.enable()
        self.remote.ws.send.side_effect = OSError("test disconnect")
        self.assertFalse(main._run_auto_step(generation, {"1": 40, "3": 0}, 0))
        self.assertFalse(main.AUTO_MODE)

    def test_stop_waits_for_inflight_step_then_prevents_restart(self):
        generation = self.enable()
        sending = threading.Event()
        release_send = threading.Event()
        stopping = threading.Event()
        worker_result = []
        stop_result = []

        def blocked_send(payload):
            if json.loads(payload)["data"]["motors"]["1"]:
                sending.set()
                if not release_send.wait(2):
                    raise TimeoutError("test barrier timeout")
            self.frames.append(json.loads(payload))

        def stop():
            stopping.set()
            stop_result.append(main.stop_all())

        self.remote.ws.send.side_effect = blocked_send
        worker = threading.Thread(target=lambda: worker_result.append(
            main._run_auto_step(generation, {"1": 40, "3": 0}, 30)
        ), daemon=True)
        stopper = threading.Thread(target=stop, daemon=True)
        worker.start()
        try:
            self.assertTrue(sending.wait(2))
            stopper.start()
            self.assertTrue(stopping.wait(2))
        finally:
            release_send.set()
            worker.join(2)
            if stopper.ident is not None:
                stopper.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(worker_result, [False])
        self.assertEqual(len(stop_result), 1)
        self.assertEqual(self.motors(), [{"1": 40, "3": 0}, {"1": 0, "3": 0}])
        self.assertFalse(main._run_auto_step(generation, {"1": 50, "3": 50}, 0))
        self.assertEqual(len(self.frames), 2)

    def test_mcp_tools_expose_independent_stop_and_stop_all(self):
        tools = {tool.name: tool for tool in asyncio.run(main.mcp.list_tools())}
        self.assertIn("stop_all", tools)
        self.assertIn("仅关闭指定通道", tools["control_device"].description)
        result = asyncio.run(main.mcp.call_tool("control_device", {"motor": 3, "intensity": 0}))
        self.assertIn("已发送", result[0].text)
        result = asyncio.run(main.mcp.call_tool("stop_all", {}))
        self.assertIn("已发送", result[0].text)
        self.assertEqual(self.motors(), [{"3": 0}, {"1": 0, "3": 0}])

    def test_streamable_http_initializes_and_calls_stop_tools(self):
        def message(response):
            self.assertEqual(response.status_code, 200)
            return next(json.loads(line[6:]) for line in response.text.splitlines()
                        if line.startswith("data: "))

        with TestClient(main.mcp.streamable_http_app()) as client:
            headers = {"Accept": "application/json, text/event-stream"}
            response = client.post("/mcp/", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "offline-test", "version": "1"}},
            })
            initialized = message(response)
            headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
            headers["Mcp-Protocol-Version"] = initialized["result"]["protocolVersion"]
            response = client.post("/mcp/", headers=headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })
            self.assertEqual(response.status_code, 202)
            response = client.post("/mcp/", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })
            self.assertIn("stop_all", {tool["name"] for tool in message(response)["result"]["tools"]})
            calls = (
                ("control_device", {"motor": 1, "intensity": 0}),
                ("control_device", {"motor": 3, "intensity": 0}),
                ("stop_all", {}),
            )
            for request_id, (name, arguments) in enumerate(calls, start=3):
                response = client.post("/mcp/", headers=headers, json={
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                })
                result = message(response)["result"]
                self.assertFalse(result.get("isError", False))
                self.assertIn("已发送", result["content"][0]["text"])
        self.assertEqual(self.motors(), [{"1": 0}, {"3": 0}, {"1": 0, "3": 0}])

    def test_procfile_entrypoint_starts_threads_and_mcp(self):
        with patch("threading.Thread.start") as start, patch("requests.post") as post, \
                patch("mcp.server.fastmcp.FastMCP.run") as run:
            runpy.run_path(main.__file__, run_name="__main__")
        self.assertEqual(start.call_count, 2)
        post.assert_not_called()
        run.assert_called_once_with(transport="streamable-http")


if __name__ == "__main__":
    unittest.main()
