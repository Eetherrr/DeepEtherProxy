# DeepSeek 网页版上游协议（实测记录）

> 实测时间：**2026-10-21**　实测环境：MaiBot 1.3.11 / Python 3.12 / `deepseek-pow` 0.0.1 / wasmtime 49
> 本文只记录**实际打通的报文形态**，与 `docs/FEASIBILITY.md` §3 的推断有出入的地方单独标注。
> 上游随时可能改版，改版时按 `deepether/constants.py` 集中修改。

## 一次调用的完整链路（实测可用）

```
① POST /api/v0/chat_session/create          {}                      → 取 chat_session.id
② POST /api/v0/chat/create_pow_challenge    {"target_path": "…"}    → 取 biz_data.challenge
③ 本地求解 PoW（wasmtime，约 0.1~0.3s）
④ POST /api/v0/chat/completion              SSE 流                  → 正文 + 思考
```

鉴权：请求头 `Authorization: Bearer <localStorage.userToken>`（**不是 cookie**）。

### ⚠️ 关键坑：token 失效也是 HTTP 200

```jsonc
POST /api/v0/chat_session/create  → HTTP 200
{"code": 40003, "msg": "INVALID_TOKEN", "data": null}

GET /api/v0/users/current        → HTTP 200
{"code": 40003, "msg": "Authorization Failed (invalid token)", "data": null}
```

- 和 PoW 的 40301 同一类问题：**必须解析 body 的 `code`，不能只看 HTTP 状态码**。
- 只看状态码会有两个后果：① 报错文案变成"建会话接口可能已变更"（误导排查方向）；
  ② 凭证不会被标记成失效，`/deepether-status` 仍显示"已就绪"（违反 §6.6.1 凭证层）。
- 现在由 `web_client._raise_for_envelope` 统一处理：`code != 0` 一律转中文错误，
  其中 `40003` / `INVALID_TOKEN` / `AUTHORIZATION FAILED` 归类为 `credential_invalid`
  并回调 `on_credential_invalid` 落盘失效标记。

## ① 建会话的真实响应结构

```jsonc
{"code": 0, "msg": "", "data": {"biz_code": 0, "biz_msg": "",
  "biz_data": {"chat_session": {"id": "1b36f6a7-…", "title": null, "model_type": "default",
                                "seq_id": 214676139, "is_empty": true, ...},
               "ttl_seconds": 259200}}}
```

→ session id 在 **`data.biz_data.chat_session.id`**。文档没写具体路径，这是实测结论。

## ② PoW 挑战的真实响应

```jsonc
{"code": 0, "data": {"biz_data": {"challenge": {
  "algorithm": "DeepSeekHashV1",
  "challenge": "b8d9d950…(64 hex)", "salt": "1f5d0a49…(20 hex)",
  "signature": "b2bdcd6a…", "difficulty": 144000,
  "expire_at": 1792513365042, "expire_after": 300000,
  "target_path": "/api/v0/chat/completion"}}}}
```

### ⚠️ 关键坑：请求头必须带 `target_path`

提交格式是 `base64(紧凑JSON)` 放进请求头 `x-ds-pow-response`：

```json
{"algorithm":"DeepSeekHashV1","challenge":"…","salt":"…","answer":122804,"signature":"…","target_path":"/api/v0/chat/completion"}
```

- **少了 `target_path` → 上游返回 200 但 body 是 `{"code":40301,"msg":"INVALID_POW_RESPONSE"}`**
  （HTTP 状态码是 200，不能用状态码判断失败，必须解析 body 的 `code`）。
- `answer` 用**整数形态**输出（wasm 返回 f64，`122804.0` 会被拒绝）。
- PoW 一次性、约 300 秒过期，每次请求都要重新领。

## ③ completion 的请求体（实测可用）

```jsonc
{"chat_session_id": "…", "prompt": "…", "thinking_enabled": true,
 "search_enabled": false, "model_type": "default",
 "parent_message_id": null, "ref_file_ids": []}
```

## ④ SSE 响应流：**不是**纯 JSON-Patch，是混合帧

这是与文档 §3.5 描述**出入最大**的地方。实测一锅混合了三种帧：

```
{"request_message_id":1,"response_message_id":2,"model_type":"default"}      ← 元信息
{"v": "我要"}                                                               ← 裸文本增量（最多）
{"v": {"response": {"fragments": [{"id":2,"type":"THINK","content":"我要"}],
                    "status": "WIP", "accumulated_token_usage": 0}}}          ← 全量状态快照
{"p":"response/fragments/-1/content","o":"APPEND","v":"回答"}                  ← 追加到最后片段
{"p":"response/fragments","o":"APPEND",
        "v":[{"id":3,"type":"RESPONSE","content":"…"}]}                       ← 新开片段（THINK→RESPONSE 分界）
{"p":"response","o":"BATCH","v":[{"p":"accumulated_token_usage","v":93},
                                 {"p":"quasi_status","v":"FINISHED"}]}        ← 批量补丁
{"p":"response/status","o":"SET","v":"FINISHED"}                              ← 结束
```

要点：

1. **裸增量 `{"v": "文本"}` 没有路径**，所以不能靠"路径里有没有 thinking"来分类；
   正确做法是维护「当前片段类型」——快照/片段追加告诉我们现在是 `THINK` 还是 `RESPONSE`，
   随后的裸增量归到该类型。实现见 `deepether/upstream/sse.py`。
2. 快照是**全量**的，增量是**局部**的，混在一起必须做「前缀重同步」，否则会重复累加。
3. `usage` 只有 `accumulated_token_usage`（BATCH 里），没有 prompt/completion 拆分。
4. 结束判定：`response/status = FINISHED` / `quasi_status = FINISHED`。

## 实测性能（2026-10-21，单账号）

| 阶段 | 耗时 |
|---|---|
| chat_session/create | ~600 ms |
| create_pow_challenge | ~220 ms |
| PoW 求解 | ~100 ms |
| completion（首字到结束） | 0.8 ~ 1.7 s |
| **插件内端到端**（建会话→PoW→读完流） | **1.9 ~ 2.9 s** |

远低于 §7.1 要求的 15 s，余量充足。

## 与文档 §3 的差异汇总

| 文档说法 | 实测 |
|---|---|
| §3.5 响应是 JSON-Patch 增量 | **混合帧**：裸 `{"v": text}` 为主 + 全量快照 + patch |
| §3.3 头部含 `target_path` | **确认必须**，漏了就是 40301（HTTP 200） |
| §3.4 请求体扁平 | 确认 |
| §3.6 usage 只有总量 | 确认（`accumulated_token_usage`） |
| 建会话返回体结构 | 文档未给；实测在 `data.biz_data.chat_session.id` |
| 联网搜索 | 文档未细说；实测多出 `TOOL_SEARCH` 片段 + 引用角标，见 §⑤ |
## ⑤ 联网搜索（`search_enabled: true`，2026-10-21 实测）

搜索**可用**，但流里会多出一类片段，必须特殊处理：

```
{"v": {"response": {"fragments": [{"id":2,"type":"THINK","content":"…"}], "status":"WIP"}}}
{"p": "response", "o": "BATCH", "v": [{"p":"fragments","o":"APPEND","v":[{"id":3,"type":"TOOL_SEARCH",
    "status":"WIP","content":null,"queries":[…],"results":[]}]}]}
{"p": "response/fragments/-1/results", "o": "SET", "v": [{"url":"…","title":"…","snippet":"…"}]}
{"p": "response/fragments/-1", "o": "BATCH", "v": [{"p":"status","v":"FINISHED"},
    {"p":"content","v":"Found 19 web pages"}]}       ← 片段级 FINISHED，不是整个响应结束
…随后才是 RESPONSE 片段的正文…
```

三个坑：

1. **`TOOL_SEARCH` 片段的 `content` 是进度文案**（Found 19 web pages），
   绝不能当正文返回；插件里归到 `ignored` 桶直接丢弃。
2. **片段内部的 `status: FINISHED` 只代表该片段结束**。若误置全局 finished 会提前掐断流，
   表现就是「只返回 Found 19 web pages、没有正文」。实现上要用 `fragment_scoped` 标记区分。
3. **正文里会带网页版内部引用角标** `[citation:1]` / `[reference:0]`，
   插件在 `adapter_out.strip_web_artifacts()` 里统一剥掉再返回给 Host。

实测耗时：搜索模式 completion 1.5~4 s（比不搜索多约 1~2 s），事件数 270~409（不搜索约 50~130）。

> 默认值仍是 `search_enabled = false`：一期目标是省 token，搜索会引入外部内容与额外延迟，需要时再打开。

