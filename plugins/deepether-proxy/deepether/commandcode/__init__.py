"""CommandCode Go 套餐反代通道。

模块边界：
- `constants` / `protocol`：上游协议（端点、头、信封、NDJSON、错误分类）；
- `client`：HTTP 与流读取；
- `adapter_in` / `adapter_out`：Host `context_items` ↔ wire messages / `output_items`；
- `catalog`：Go 模型目录；`budget`：金额窗口；`pacing`：节奏；`state`：统计；
- `provider`：把上面这些组装成一个 `LLMProviderBase`；
- `status`：命令文本渲染。
"""
