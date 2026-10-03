# Kisstoy MCP 控制器

通过云端 WebSocket 控制振动（通道 1）和吮吸（通道 3）。

## 控制方法

| 操作 | MCP 工具与参数 |
| --- | --- |
| 调整振动 | `control_device(motor=1, intensity=40)` |
| 只关闭振动 | `control_device(motor=1, intensity=0)` |
| 调整吮吸 | `control_device(motor=3, intensity=60)` |
| 只关闭吮吸 | `control_device(motor=3, intensity=0)` |
| 全部停止 / 急停 | `stop_all()` |
| 开启自动挂机 | `set_auto_pilot(enable=true)` |
| 关闭自动挂机并停止两路 | `set_auto_pilot(enable=false)` |

强度范围为 0–100。手动控制会取消自动挂机，只向指定通道发送指令，另一通道保持原状态。更新后，`intensity=0` 改为单路关闭；需要全部停止时应调用 `stop_all()`。

全部停止在一条消息中发送 `motors={"1": 0, "3": 0}`，自动挂机每一步也同时发送两路状态。手动控制和自动步骤使用同一把锁；取消或重新开启挂机时，旧步骤会失效，避免在停止后继续启动。

工具返回“已发送”仅表示 WebSocket 发送成功，当前协议未处理设备执行确认。离线或发送异常会明确返回失败；全部停止发送失败时，自动挂机仍会取消，但设备是否停止需要实际确认。

## 运行

```sh
python -m pip install -r requirements.txt
python main.py
```

部署保留原来的 `Procfile` 和 streamable HTTP MCP 服务。通过环境变量设置 `DEVICE_ID`、`GROUP`、`SHARE_ID`，以及 `PORT`（默认 8000）或 `FASTMCP_PORT`。`update_share_id(new_id)` 可以热更新分享 ID。

仅执行 `python main.py` 才会启动设备连接和自动挂机线程，导入模块不会进行授权绑定或连接真实设备。

## 验证

```sh
python -m unittest discover -s tests -v
```

测试使用模拟 WebSocket，不连接或操作实物设备，覆盖单路关闭、两路一次归零、挂机取消及快速重启、停止与发送并发、离线/异常返回、参数校验和 MCP 工具调用。实物仍需确认两路归零消息是否被云端和设备正确执行。
