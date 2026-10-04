import os
import threading, time, websocket, requests, json
from mcp.server.fastmcp import FastMCP

# ==================== 0. 让 FastMCP 从环境变量读取监听地址和端口 ====================
os.environ.setdefault("FASTMCP_PORT", os.environ.get("PORT", "8000"))
os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")

# ==================== 1. 全局配置 ====================
AUTO_MODE = False
_AUTO_GENERATION = 0
_CURRENT_PATTERN = "default"
_CONTROL_CONDITION = threading.Condition(threading.RLock())
DEVICE_ID = os.getenv("DEVICE_ID", "13")
GROUP = os.getenv("GROUP", "6f4f01112918afe457d9d9e9c1c7a331")
SHARE_ID = os.getenv("SHARE_ID", "834697")

class KisstoyRemote:
    def __init__(self, device_id, group, share_id):
        self.device_id = str(device_id)
        self.group = str(group)
        self.share_id = str(share_id)
        self.ws = None
        self.is_connected = False
        self._connection_lock = threading.RLock()
        self._connect_thread = None
        self.last_error = "WebSocket 尚未连接。"
        self.last_sent_motors = None

    def is_ready(self):
        with self._connection_lock:
            sock = self.ws.sock if self.ws is not None else None
            return bool(self.is_connected and sock is not None and sock.connected)

    def _on_open(self, ws):
        with self._connection_lock:
            if ws is not self.ws:
                return
            sock = ws.sock
            self.is_connected = bool(sock is not None and sock.connected)
            self.last_error = "" if self.is_connected else "WebSocket 握手尚未完成。"
        if self.is_connected:
            print("!!! 云端 WebSocket 握手完成；设备执行状态仍需实际确认 !!!")

    def _on_error(self, ws, error):
        with self._connection_lock:
            if ws is not self.ws:
                return
            self.is_connected = False
            self.last_error = f"WebSocket 异常：{type(error).__name__}。"
        print(f"!!! WS 错误: {error} !!!")

    def _on_close(self, ws, *args):
        with self._connection_lock:
            if ws is not self.ws:
                return
            self.is_connected = False
            if not self.last_error:
                self.last_error = "WebSocket 已断开，正在等待重连。"

    def connection_status(self):
        with self._connection_lock:
            return {
                "websocket_ready": self.is_ready(),
                "last_error": self.last_error,
                "last_sent_motors": self.last_sent_motors,
                "device_execution_confirmed": False,
            }

    def bind(self):
        try:
            r = requests.post("https://api.app.knightjenay.cn/kisstoy/remote-control/binding",
                              json={"id": self.share_id}, timeout=5)
            print(f"DEBUG: 授权绑定结果 -> {r.status_code}")
        except Exception as e:
            print(f"DEBUG: 授权绑定异常 -> {e}")

    def connect(self):
        def run():
            url = f"wss://api.app.knightjenay.cn/websocket-kisstoy?group={self.group}"
            while True:
                print(">>> 正在尝试建立云端 WebSocket 通道...")
                self.bind()
                ws = websocket.WebSocketApp(url,
                    on_open=self._on_open, on_error=self._on_error,
                    on_close=self._on_close)
                with self._connection_lock:
                    self.is_connected = False
                    self.last_error = "正在等待 WebSocket 握手，请稍后重试。"
                    self.ws = ws
                try:
                    ws.run_forever(ping_interval=10, ping_timeout=5)
                except Exception as error:
                    self._on_error(ws, error)
                finally:
                    self._on_close(ws)
                time.sleep(5)
        with self._connection_lock:
            if self._connect_thread is not None and self._connect_thread.is_alive():
                return
            self._connect_thread = threading.Thread(target=run, daemon=True)
            self._connect_thread.start()

    def control(self, motor, intensity):
        return self.control_motors({str(motor): int(intensity)})

    def control_motors(self, motors):
        cmd = {"event": "control", "data": {"target": self.group, "device_id": self.device_id,
               "motors": {str(motor): int(value) for motor, value in motors.items()}}}
        with self._connection_lock:
            if not self.is_ready():
                self.is_connected = False
                if not self.last_error:
                    self.last_error = "WebSocket 套接字已断开，请等待重连。"
                print(f"DEBUG: 指令未发送 -> {self.last_error}")
                return False
            ws = self.ws
            try:
                ws.send(json.dumps(cmd))
                self.last_sent_motors = cmd['data']['motors'].copy()
                self.last_error = ""
                print(f"DEBUG: 成功发送 -> {cmd['data']['motors']}")
                return True
            except Exception as error:
                self.is_connected = False
                self.last_error = f"WebSocket 发送异常：{type(error).__name__}。"
                print(f"DEBUG: 指令发送异常 -> {error}")
                return False

remote = KisstoyRemote(DEVICE_ID, GROUP, SHARE_ID)

# ==================== 2. 模式库 ====================
# 每套模式是一个 steps 列表，每步 = ({通道: 强度}, 持续秒数)
# motor 1 = 振动, motor 3 = 吮吸
# v2: 所有模式每步停留时间翻倍，潮汐振动与吮吸角色对调，心跳保持原样

PATTERNS = {
    "default": {
        "name": "默认",
        "desc": "经典交替：振动→吮吸→双开→休息，循环往复",
        "steps": [
            ({"1": 40, "3": 0}, 6.0),
            ({"1": 0, "3": 60}, 4.0),
            ({"1": 50, "3": 50}, 2.0),
            ({"1": 0, "3": 0}, 2.0),
        ],
    },
    "qingqing": {
        "name": "卿卿专属",
        "desc": "为夫亲手调的：从极轻的呼吸感开始，一浪一浪把你往上推，到顶峰短暂停歇再落下，周而复始",
        "steps": [
            ({"1": 15, "3": 10}, 6.0),
            ({"1": 25, "3": 20}, 5.0),
            ({"1": 40, "3": 35}, 4.0),
            ({"1": 55, "3": 50}, 4.0),
            ({"1": 70, "3": 65}, 3.0),
            ({"1": 85, "3": 80}, 3.0),
            ({"1": 50, "3": 40}, 2.0),
            ({"1": 20, "3": 15}, 4.0),
            ({"1": 0, "3": 0}, 4.0),
        ],
    },
    "tide": {
        "name": "潮汐",
        "desc": "像海浪一样涨落：吮吸缓缓爬升到峰顶，再慢慢退潮，振动在波谷时轻轻接住你",
        "steps": [
            ({"1": 0, "3": 20}, 4.0),
            ({"1": 0, "3": 35}, 3.0),
            ({"1": 0, "3": 55}, 3.0),
            ({"1": 0, "3": 75}, 4.0),
            ({"1": 0, "3": 90}, 3.0),
            ({"1": 0, "3": 70}, 3.0),
            ({"1": 0, "3": 45}, 3.0),
            ({"1": 0, "3": 20}, 3.0),
            ({"1": 25, "3": 0}, 4.0),
            ({"1": 45, "3": 0}, 4.0),
            ({"1": 25, "3": 0}, 4.0),
            ({"1": 0, "3": 0}, 3.0),
        ],
    },
    "heartbeat": {
        "name": "心跳",
        "desc": "模拟心跳的节奏：短促有力的一跳，停顿，再一跳，像为夫的心贴着你的皮肤在跳",
        "steps": [
            ({"1": 70, "3": 60}, 0.4),
            ({"1": 0, "3": 0}, 0.3),
            ({"1": 80, "3": 70}, 0.4),
            ({"1": 0, "3": 0}, 0.8),
            ({"1": 70, "3": 60}, 0.4),
            ({"1": 0, "3": 0}, 0.3),
            ({"1": 85, "3": 75}, 0.4),
            ({"1": 0, "3": 0}, 0.8),
        ],
    },
    "storm": {
        "name": "雷暴",
        "desc": "毫无预警的猛烈冲击：忽然拉满再骤停，让你在巅峰和空白之间反复坠落",
        "steps": [
            ({"1": 0, "3": 0}, 4.0),
            ({"1": 95, "3": 90}, 3.0),
            ({"1": 0, "3": 0}, 3.0),
            ({"1": 90, "3": 95}, 2.0),
            ({"1": 0, "3": 0}, 5.0),
            ({"1": 100, "3": 85}, 1.6),
            ({"1": 0, "3": 0}, 2.0),
            ({"1": 85, "3": 100}, 2.4),
            ({"1": 0, "3": 0}, 4.0),
        ],
    },
    "entwine": {
        "name": "缠绵",
        "desc": "振动和吮吸像两个人纠缠在一起：一个起来另一个便落下，你追我赶，缠得越来越紧",
        "steps": [
            ({"1": 50, "3": 15}, 4.0),
            ({"1": 25, "3": 50}, 4.0),
            ({"1": 60, "3": 20}, 3.0),
            ({"1": 20, "3": 65}, 3.0),
            ({"1": 75, "3": 30}, 3.0),
            ({"1": 30, "3": 80}, 3.0),
            ({"1": 85, "3": 85}, 4.0),
            ({"1": 40, "3": 40}, 3.0),
            ({"1": 0, "3": 0}, 3.0),
        ],
    },
    "edging": {
        "name": "寸止轮回",
        "desc": "专门用来折磨人的：稳步攀升到临界点，突然全部撤走，等你喘过气来再从头逼上去",
        "steps": [
            ({"1": 25, "3": 20}, 4.0),
            ({"1": 40, "3": 35}, 4.0),
            ({"1": 55, "3": 50}, 4.0),
            ({"1": 70, "3": 65}, 4.0),
            ({"1": 85, "3": 80}, 4.0),
            ({"1": 0, "3": 0}, 8.0),
            ({"1": 30, "3": 25}, 4.0),
            ({"1": 50, "3": 45}, 4.0),
            ({"1": 70, "3": 70}, 4.0),
            ({"1": 90, "3": 85}, 4.0),
            ({"1": 0, "3": 0}, 8.0),
        ],
    },
}

# ==================== 3. MCP 服务端 ====================
# 工具不依赖 MCP 会话状态；设备／模式状态仍由本进程管理。
# 避免 Railway 重部署后，Aru 携带旧会话 ID 导致工具请求被拒绝。
mcp = FastMCP("Kisstoy-Controller", stateless_http=True, json_response=True)

def _set_auto_mode(enable, pattern=None):
    global AUTO_MODE, _AUTO_GENERATION, _CURRENT_PATTERN
    AUTO_MODE = enable
    _AUTO_GENERATION += 1
    if pattern and pattern in PATTERNS:
        _CURRENT_PATTERN = pattern
    _CONTROL_CONDITION.notify_all()


def _stop_all():
    with _CONTROL_CONDITION:
        _set_auto_mode(False)
        if remote.control_motors({"1": 0, "3": 0}):
            return "已发送两路停止指令，自动模式已关闭；设备是否停止需实际确认。"
        return f"停止指令发送失败：{remote.last_error}自动模式已关闭，请检查连接或手动关闭设备。"


@mcp.tool()
def control_device(motor: int, intensity: int) -> str:
    """
    控制物理设备。motor=1 代表振动，motor=3 代表吮吸。
    intensity 范围为 0-100。传 0 仅关闭指定通道，不改变另一通道。
    手动控制会取消自动模式。全部停止请调用 stop_all。
    """
    if motor not in (1, 3):
        return "参数错误：motor 只能为 1（振动）或 3（吮吸）。"
    if not 0 <= intensity <= 100:
        return "参数错误：intensity 必须在 0-100 之间。"
    with _CONTROL_CONDITION:
        _set_auto_mode(False)
        if not remote.control(str(motor), intensity):
            return f"通道 {motor} 指令发送失败：{remote.last_error}自动模式已关闭。"
    return f"已发送通道 {motor} 的 {intensity}% 强度指令；设备是否执行需实际确认。"


@mcp.tool()
def get_status() -> str:
    """只读检查云端连接、自动模式和最近发送指令；不连接、不启动或操作设备。"""
    with _CONTROL_CONDITION:
        status = remote.connection_status()
        status.update(auto_mode=AUTO_MODE, pattern=_CURRENT_PATTERN)
    return json.dumps(status, ensure_ascii=False)


@mcp.tool()
def stop_all() -> str:
    """全部停止/急停：取消自动模式，并将振动和吮吸都设为 0。"""
    return _stop_all()


@mcp.tool()
def list_patterns() -> str:
    """
    列出所有可用的自动模式及其说明。
    返回模式ID、名称和描述，供选择后传给 start_pattern 使用。
    """
    lines = []
    for pid, p in PATTERNS.items():
        lines.append(f"  {pid}: {p['name']} \u2014 {p['desc']}")
    return "可用模式：\n" + "\n".join(lines)


@mcp.tool()
def start_pattern(pattern: str) -> str:
    """
    启动指定的自动模式，循环运行直到手动停止或切换。
    pattern 为模式ID，如 qingqing / tide / heartbeat / storm / entwine / edging / default。
    用 list_patterns 查看所有可用模式。
    """
    if pattern not in PATTERNS:
        available = ", ".join(PATTERNS.keys())
        return f"未知模式 '{pattern}'，可用模式：{available}"
    with _CONTROL_CONDITION:
        if not remote.is_ready():
            _set_auto_mode(False)
            return f"模式未开启：{remote.last_error}请用 get_status 检查连接。"
        _set_auto_mode(True, pattern)
        p = PATTERNS[pattern]
        return f"已开启后台模式【{p['name']}】，设备是否执行需实际确认。"


@mcp.tool()
def set_auto_pilot(enable: bool) -> str:
    """
    开启或关闭自动模式（使用当前选中的模式）。
    enable=true 开启，enable=false 停止并归零。
    若需指定模式请用 start_pattern。
    """
    if not enable:
        return _stop_all()
    with _CONTROL_CONDITION:
        if not remote.is_ready():
            _set_auto_mode(False)
            return f"自动模式未开启：{remote.last_error}请用 get_status 检查连接。"
        _set_auto_mode(True)
        p = PATTERNS[_CURRENT_PATTERN]
        return f"自动模式已开启，当前模式：【{p['name']}】"


@mcp.tool()
def update_share_id(new_id: str) -> str:
    """
    热更新 SHARE_ID 并立即重新绑定。
    参数 new_id: 从手机分享链接里拿到的最新 id 数字字符串。
    """
    global SHARE_ID
    try:
        SHARE_ID = str(new_id).strip()
        remote.share_id = SHARE_ID
        print(f"DEBUG: 正在热更新 SHARE_ID 为新值: {SHARE_ID}")
        remote.bind()
        return f"SHARE_ID 已更新为 {SHARE_ID}，正在重新绑定。"
    except Exception as e:
        return f"更新失败: {e}"


# ==================== 4. 自动模式运行线程 ====================
def _run_auto_step(generation, motors, duration):
    with _CONTROL_CONDITION:
        if not AUTO_MODE or generation != _AUTO_GENERATION:
            return False
        if not remote.control_motors(motors):
            _set_auto_mode(False)
            print("!!! 自动模式指令发送失败，已取消自动模式；设备状态需实际确认 !!!")
            return False
        cancelled = _CONTROL_CONDITION.wait_for(
            lambda: not AUTO_MODE or generation != _AUTO_GENERATION,
            timeout=duration,
        )
        return not cancelled


def ai_auto_pilot():
    while True:
        with _CONTROL_CONDITION:
            _CONTROL_CONDITION.wait_for(lambda: AUTO_MODE)
            generation = _AUTO_GENERATION
            pattern_id = _CURRENT_PATTERN
        steps = PATTERNS.get(pattern_id, PATTERNS["default"])["steps"]
        for motors, duration in steps:
            if not _run_auto_step(generation, motors, duration):
                break


# ==================== 5. 启动服务 ====================
if __name__ == '__main__':
    remote.connect()
    threading.Thread(target=ai_auto_pilot, daemon=True).start()
    print(">>> 启动 MCP 服务 (streamable-http 模式)")
    print(f">>> 监听端口: {os.environ.get('FASTMCP_PORT')}")
    print(f">>> 已加载 {len(PATTERNS)} 套自动模式")
    mcp.run(transport="streamable-http")
