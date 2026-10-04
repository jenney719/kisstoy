import asyncio
import importlib
import json
import runpy
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import websocket
from starlette.testclient import TestClient


# 导入不得连接真实设备、授权绑定或启动后台线程。
with patch("threading.Thread.start") as import_start, patch("requests.post") as import_post:
    main = importlib.import_module("main")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.remote = main.KisstoyRemote("test-device", "test-group", "test-share")
        self.frames = []
        self.remote.ws = Mock()
        self.remote.ws.sock.connected = True
        self.remote.ws.send.side_effect = lambda payload: self.frames.append(json.loads(payload))
        self.remote._on_open(self.remote.ws)
        self.remote_patch = patch.object(main, "remote", self.remote)
        self.remote_patch.start()
        self.addCleanup(self.remote_patch.stop)
        with main._CONTROL_CONDITION:
            main._set_auto_mode(False, "default")

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
        for motors, _ in main.PATTERNS["default"]["steps"]:
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
            return response.json()

        with TestClient(main.mcp.streamable_http_app()) as client:
            headers = {"Accept": "application/json, text/event-stream"}
            response = client.post("/mcp/", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "offline-test", "version": "1"}},
            })
            initialized = message(response)
            self.assertNotIn("mcp-session-id", response.headers)
            headers["Mcp-Session-Id"] = "session-from-previous-deployment"
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

    def test_tools_wait_for_completed_handshake(self):
        self.remote.ws.sock.connected = False
        self.remote.is_connected = True  # 模拟过期的布尔状态。
        self.assertIn("未开启", main.start_pattern("default"))
        self.assertFalse(main.AUTO_MODE)
        self.assertIn("未开启", main.set_auto_pilot(True))
        self.assertIn("发送失败", main.control_device(1, 30))
        self.remote.ws.send.assert_not_called()
        self.remote.ws.sock.connected = True
        self.remote._on_open(self.remote.ws)
        self.assertIn("已发送", main.control_device(1, 30))
        self.assertEqual(self.motors(), [{"1": 30}])

    def test_old_socket_callbacks_cannot_break_reconnected_socket(self):
        old_ws = self.remote.ws
        new_ws = Mock()
        new_ws.sock.connected = True
        new_ws.send.side_effect = lambda payload: self.frames.append(json.loads(payload))
        self.remote.ws = new_ws
        self.remote._on_open(new_ws)
        self.remote._on_error(old_ws, OSError("old disconnect"))
        self.remote._on_close(old_ws)
        self.assertTrue(self.remote.is_ready())
        self.assertIn("已发送", main.control_device(3, 20))
        new_ws.send.assert_called_once()
        old_ws.send.assert_not_called()

    def test_send_error_clears_connection_and_reports_reason(self):
        self.remote.ws.send.side_effect = OSError("test disconnect")
        response = main.control_device(1, 30)
        self.assertIn("OSError", response)
        self.assertFalse(self.remote.is_ready())
        self.assertFalse(self.remote.is_connected)
        self.assertIn("未开启", main.start_pattern("default"))
        self.assertFalse(main.AUTO_MODE)

    def test_connect_is_idempotent(self):
        with patch("main.threading.Thread") as thread:
            thread.return_value.is_alive.return_value = True
            self.remote.connect()
            self.remote.connect()
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_connect_publishes_ready_only_after_on_open(self):
        ws = Mock()
        ws.sock.connected = False

        def handshake(**kwargs):
            self.assertFalse(self.remote.is_ready())
            self.assertIn("未开启", main.start_pattern("default"))
            self.assertIn("发送失败", main.control_device(1, 30))
            ws.send.assert_not_called()
            ws.sock.connected = True
            self.remote._on_open(ws)
            self.assertTrue(self.remote.is_ready())
            self.assertIn("已发送", main.control_device(1, 30))
            # run_forever 返回且不调用 on_close 时，也必须清理连接状态。

        ws.run_forever.side_effect = handshake
        with patch.object(self.remote, "bind"), patch("main.websocket.WebSocketApp", return_value=ws), \
                patch("main.threading.Thread") as thread, patch("main.time.sleep", side_effect=SystemExit):
            self.remote.connect()
            run = thread.call_args.kwargs["target"]
            with self.assertRaises(SystemExit):
                run()
        self.assertFalse(self.remote.is_ready())
        ws.send.assert_called_once()
        sent = json.loads(ws.send.call_args.args[0])
        self.assertEqual(sent["data"]["motors"], {"1": 30})

    def test_control_device_sends_real_websocket_frame(self):
        # 使用本地 socketpair 验证 websocket-client 的实际发送，无设备或外网连接。
        sender, receiver = socket.socketpair()
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        receiver.settimeout(2)
        ws = websocket.WebSocketApp("ws://test.invalid")
        ws.sock = websocket.WebSocket()
        ws.sock.sock = sender
        ws.sock.connected = True
        self.remote.ws = ws
        self.remote._on_open(ws)
        self.assertIn("已发送", main.control_device(1, 30))
        def read_exact(size):
            data = b""
            while len(data) < size:
                part = receiver.recv(size - len(data))
                self.assertTrue(part)
                data += part
            return data

        header = read_exact(2)
        self.assertEqual(header[0] & 0x0F, 1)
        self.assertTrue(header[1] & 0x80)
        size = header[1] & 0x7F
        if size == 126:
            size = int.from_bytes(read_exact(2), "big")
        mask = read_exact(4)
        payload = read_exact(size)
        decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        sent = json.loads(decoded)
        self.assertEqual(sent["event"], "control")
        self.assertEqual(sent["data"]["motors"], {"1": 30})
        self.assertEqual(sent["data"]["target"], "test-group")

    def test_actual_auto_thread_does_not_send_during_startup_handshake(self):
        # 在独立进程中运行实际挂机线程，避免守护线程泄漏到其他测试。
        code = """
import json, threading
from unittest.mock import Mock
import main
remote = main.KisstoyRemote('test-device', 'test-group', 'test-share')
remote.ws = Mock()
remote.ws.sock = None
main.remote = remote
idle = threading.Event()
sent = threading.Event()
frames = []
real_wait = main._CONTROL_CONDITION.wait_for
def wait(predicate, timeout=None):
    if not main.AUTO_MODE:
        idle.set()
    return real_wait(predicate, timeout)
main._CONTROL_CONDITION.wait_for = wait
def send(payload):
    frames.append(json.loads(payload)['data']['motors'])
    sent.set()
remote.ws.send.side_effect = send
worker = threading.Thread(target=main.ai_auto_pilot, daemon=True)
worker.start()
assert idle.wait(2), 'auto worker never became idle'
assert not frames, 'auto worker sent before handshake'
assert not main.AUTO_MODE
assert '未开启' in main.start_pattern('default')
assert not frames
remote.ws.sock = Mock(connected=True)
remote._on_open(remote.ws)
assert '已开启后台模式' in main.start_pattern('default')
assert sent.wait(2), 'auto worker did not send after handshake'
main.stop_all()
assert frames[0] == {'1': 40, '3': 0}
assert frames[-1] == {'1': 0, '3': 0}
assert not main.AUTO_MODE
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sdk_stateful_reference_reproduces_old_session_400(self):
        stateful = main.FastMCP("stateful-reference", stateless_http=False)
        with TestClient(stateful.streamable_http_app()) as client:
            response = client.post("/mcp/", headers={
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": "session-from-previous-deployment",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("No valid session ID", response.text)

    def test_all_seven_patterns_keep_valid_commands_and_cancel_correctly(self):
        self.assertEqual(set(main.PATTERNS), {
            "default", "qingqing", "tide", "heartbeat", "storm", "entwine", "edging",
        })
        for pattern, settings in main.PATTERNS.items():
            with self.subTest(pattern=pattern):
                self.frames.clear()
                self.assertIn("已开启后台模式", main.start_pattern(pattern))
                generation = main._AUTO_GENERATION
                self.assertEqual(main._CURRENT_PATTERN, pattern)
                for motors, duration in settings["steps"]:
                    self.assertEqual(set(motors), {"1", "3"})
                    self.assertTrue(all(0 <= intensity <= 100 for intensity in motors.values()))
                    self.assertGreater(duration, 0)
                    self.assertTrue(main._run_auto_step(generation, motors, 0))
                main.control_device(1, 0)
                self.assertFalse(main._run_auto_step(generation, {"1": 50, "3": 50}, 0))
                self.assertFalse(main.AUTO_MODE)
                self.assertEqual(self.motors()[-1], {"1": 0})

    def test_get_status_is_read_only_and_reports_sends_without_execution_claim(self):
        self.enable()
        generation = main._AUTO_GENERATION
        status = json.loads(main.get_status())
        self.assertTrue(status["websocket_ready"])
        self.assertTrue(status["auto_mode"])
        self.assertFalse(status["device_execution_confirmed"])
        self.assertIsNone(status["last_sent_motors"])
        self.assertEqual(main._AUTO_GENERATION, generation)
        self.remote.ws.send.assert_not_called()
        main.control_device(3, 20)
        status = json.loads(main.get_status())
        self.assertEqual(status["last_sent_motors"], {"3": 20})
        self.assertFalse(status["auto_mode"])

    def test_procfile_entrypoint_starts_threads_and_mcp(self):
        with patch("threading.Thread.start") as start, patch("requests.post") as post, \
                patch("mcp.server.fastmcp.FastMCP.run") as run:
            runpy.run_path(main.__file__, run_name="__main__")
        self.assertEqual(start.call_count, 2)
        post.assert_not_called()
        run.assert_called_once_with(transport="streamable-http")


if __name__ == "__main__":
    unittest.main()
