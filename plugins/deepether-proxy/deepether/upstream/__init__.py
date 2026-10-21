"""DeepSeek 网页版上游调用层。

**所有针对上游协议的假设都集中在这个包里**：路径与字段名在 `../constants.py`，
PoW 求解在 `pow.py`，SSE 增量解析在 `sse.py`，HTTP 编排在 `web_client.py`。
上游改版时只需要改这三处。
"""
