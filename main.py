import os
import threading, time, websocket, requests, json
from mcp.server.fastmcp import FastMCP

# ==================== 0. 让 FastMCP 从环境变量读取监听地址和端口 ====================
# Railway 会自动注入 PORT 环境变量，这里兜底为 8000
os.environ.setdefault("FASTMCP_PORT", os.environ.get("PORT", "8000"))
os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")

# ==================== 1. 全局配置 ====================
AUTO_MODE = False
_AUTO_GENERATION = 0
# 手动命令、停止和自动步骤使用同一把锁，防止停止后被旧步骤重新启动。
_CONTROL_CONDITION = threading.Condition(threading.RLock())
DEVICE_ID = os.getenv("DEVICE_ID", "13")
GROUP = os.getenv("GROUP", "6f4f01112918afe457d9d9e9c1c7a331")
SHARE_ID = os.getenv("SHARE_ID", "834697")  # 初始 ID，之后可用 update_share_id 热更新

class KisstoyRemote:
    def __init__(self, device_id, group, share_id):
        self.device_id = str(device_id)
        self.group = str(group)
        self.share_id = str(share_id)
        self.ws = None
        self.is_connected = False

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
                self.ws = websocket.WebSocketApp(url,
                    on_open=lambda ws: (print("!!! WS 连通成功，设备已就绪 !!!"), setattr(self, 'is_connected', True)),
                    on_error=lambda ws, e: print(f"!!! WS 错误: {e} !!!"),
                    on_close=lambda ws, *args: (print("!!! WS 断开，5秒后重连 !!!"), setattr(self, 'is_connected', False)))
                # 心跳保活，防止被云端踢掉
                self.ws.run_forever(ping_interval=10, ping_timeout=5)
                time.sleep(5)
        threading.Thread(target=run, daemon=True).start()

    def control(self, motor, intensity):
        return self.control_motors({str(motor): int(intensity)})

    def control_motors(self, motors):
        """同一条消息可更新多个通道，全部停止必须同时包含两路归零。"""
        if not self.is_connected:
            print("DEBUG: 尝试发送指令，但 WS 处于离线状态")
            return False
        cmd = {"event": "control", "data": {"target": self.group, "device_id": self.device_id,
               "motors": {str(motor): int(value) for motor, value in motors.items()}}}
        try:
            self.ws.send(json.dumps(cmd))
            print(f"DEBUG: 成功发送 -> {cmd['data']['motors']}")
            return True
        except Exception as e:
            print(f"DEBUG: 指令发送异常 -> {e}")
            return False

remote = KisstoyRemote(DEVICE_ID, GROUP, SHARE_ID)

# ==================== 2. MCP 服务端 ====================
mcp = FastMCP("Kisstoy-Controller")

def _set_auto_mode(enable):
    """调用者持有 _CONTROL_CONDITION；代数使快速关开后的旧步骤失效。"""
    global AUTO_MODE, _AUTO_GENERATION
    AUTO_MODE = enable
    _AUTO_GENERATION += 1
    _CONTROL_CONDITION.notify_all()


def _stop_all():
    with _CONTROL_CONDITION:
        _set_auto_mode(False)
        if remote.control_motors({"1": 0, "3": 0}):
            return "自动挂机已关闭，已发送振动和吮吸两路停止指令；设备是否停下需实际确认。"
        return "停止指令发送失败：自动挂机已关闭，但无法确认设备已停止，请检查连接或使用设备上的停止键。"


@mcp.tool()
def control_device(motor: int, intensity: int) -> str:
    """
    控制物理设备。motor=1 代表振动，motor=3 代表吮吸。
    intensity 范围为 0-100。传 0 仅关闭指定通道，不改变另一通道。
    手动控制会取消自动挂机。全部停止请调用 stop_all 或 set_auto_pilot(false)。
    """
    if motor not in (1, 3):
        return "参数错误：motor 只能为 1（振动）或 3（吮吸）。"
    if not 0 <= intensity <= 100:
        return "参数错误：intensity 必须在 0-100 之间。"
    with _CONTROL_CONDITION:
        _set_auto_mode(False)
        if not remote.control(str(motor), intensity):
            return f"通道 {motor} 指令发送失败：自动挂机已关闭，请检查设备连接；无法确认设备状态。"
    return f"已发送通道 {motor} 的 {intensity}% 强度指令。"


@mcp.tool()
def stop_all() -> str:
    """全部停止/急停：取消自动挂机，并在同一条消息中将振动和吮吸都设为 0。"""
    return _stop_all()

@mcp.tool()
def set_auto_pilot(enable: bool) -> str:
    """
    开启或关闭自动挂机模式（交替振动和吮吸）。
    enable=true 开启，enable=false 取消自动挂机并发送两路停止指令。
    """
    if not enable:
        return _stop_all()
    with _CONTROL_CONDITION:
        if not remote.is_connected:
            _set_auto_mode(False)
            return "自动挂机未开启：设备通道离线，请先检查连接。"
        _set_auto_mode(True)
        return "自动挂机已开启，正在按节奏运行。"

@mcp.tool()
def update_share_id(new_id: str) -> str:
    """
    当用户的分享链接 ID 发生变化时使用，热更新最新的 SHARE_ID 并立即重新绑定。
    参数 new_id: 从手机分享链接里拿到的最新 id 数字字符串。
    """
    global SHARE_ID
    try:
        SHARE_ID = str(new_id).strip()
        remote.share_id = SHARE_ID
        print(f"DEBUG: 正在热更新 SHARE_ID 为新值: {SHARE_ID}")
        remote.bind()
        return f"SHARE_ID 已更新为 {SHARE_ID}，正在用新 ID 重新绑定。"
    except Exception as e:
        return f"更新失败: {e}"

# ==================== 3. 自动驾驶线程 ====================
AUTO_STEPS = (
    ({"1": 40, "3": 0}, 3.0),
    ({"1": 0, "3": 60}, 2.0),
    ({"1": 50, "3": 50}, 1.0),
    ({"1": 0, "3": 0}, 1.0),
)


def _run_auto_step(generation, motors, duration):
    with _CONTROL_CONDITION:
        if not AUTO_MODE or generation != _AUTO_GENERATION:
            return False
        if not remote.control_motors(motors):
            _set_auto_mode(False)
            print("!!! 自动挂机指令发送失败，已取消自动挂机；设备状态需实际确认 !!!")
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
        for motors, duration in AUTO_STEPS:
            if not _run_auto_step(generation, motors, duration):
                break

# ==================== 4. 启动服务（关键修正） ====================
if __name__ == '__main__':
    remote.connect()
    threading.Thread(target=ai_auto_pilot, daemon=True).start()
    print(">>> 启动 MCP 服务 (streamable-http 模式)")
    print(f">>> 监听端口由环境变量 FASTMCP_PORT 决定: {os.environ.get('FASTMCP_PORT')}")
    # 只传 transport，绝不再传 host/port，避免 TypeError
    mcp.run(transport="streamable-http")
