# DeepEtherProxy

[![release](https://img.shields.io/github/v/release/Eetherrr/DeepEtherProxy)](https://github.com/Eetherrr/DeepEtherProxy/releases/latest)
[![license](https://img.shields.io/github/license/Eetherrr/DeepEtherProxy)](LICENSE)
![MaiBot](https://img.shields.io/badge/MaiBot-1.3.11-blue)
![python](https://img.shields.io/badge/python-3.12-blue)

把 **DeepSeek 网页版**接成 **MaiBot** 的模型 Provider —— 网页版优先，失败自动降级到 Host 已配置的官方模型。
以 **MaiBot 第三方插件** 形式提供（`plugins/deepether-proxy/`），**不修改 MaiBot 主程序**。

> 定位：**省群聊 token 的补充通道**，不是官方 API 的替代品。
> 一期只接 `replyer` + `utils`（纯文本、不吃工具调用）；`planner` 仍走官方模型。

## 快速开始

1. 从 [Releases](https://github.com/Eetherrr/DeepEtherProxy/releases/latest) 下载最新版 `deepether-proxy-<version>.zip`
2. MaiBot WebUI → 插件页 → 上传 zip 安装 → **重启一次 MaiBot**
3. WebUI → 插件 → DeepEther Proxy → 凭证，填入网页版 token：

   ```js
   // 浏览器登录 chat.deepseek.com 后，F12 → Console 执行
   copy(JSON.parse(localStorage.getItem('userToken')).value)
   ```

4. 聊天里发 `/deepether-status`，看到「凭证：已就绪」即完成

插件加载时会自动把服务商与三个模型写进 `config/model_config.toml` 并接好任务列表，
**不需要手动改配置**（详见下文「自动注册」）。完整安装/授权/排查见
[插件 README](plugins/deepether-proxy/README.md)。

## 能力边界

| 能做 | 说明 |
|---|---|
| `replyer` / `utils` 文本生成 | 纯文本、不需要工具调用；含思考内容回传 |
| 自动降级 | 网页版不可用时切到 Host 已配置的官方模型，插件内**不保存任何 API key** |
| 反封禁节奏控制 | 串行 + 抖动间隔 + 三级配额 + 会话复用 + 指纹自洽，详见 [docs/ANTIBAN.md](docs/ANTIBAN.md) |

| 做不到 | 原因 |
|---|---|
| `planner` 工具调用 | 网页版没有 function calling；插件只能"翻译"调用请求，不能替宿主执行工具 |
| 视觉 / embedding / 语音 | 网页版没有对应接口，请把这三类任务指向官方模型 |
| 逐字流式 | Host 侧插件 Provider 不支持流式回调，插件内部读完 SSE 后整体返回 |

## 文档

| 文档 | 内容 |
|---|---|
| [plugins/deepether-proxy/README.md](plugins/deepether-proxy/README.md) | 安装、授权、配置、排查（用户向） |
| [plugins/deepether-proxy/docs/upstream-protocol.md](plugins/deepether-proxy/docs/upstream-protocol.md) | 网页版协议实测记录（与文档的差异、混合帧 SSE、PoW 细节） |
| [docs/FEASIBILITY.md](docs/FEASIBILITY.md) | 可行性分析报告：结论、能力差距、架构取舍、实施路线 |
| [docs/ANTIBAN.md](docs/ANTIBAN.md) | 反封禁设计：封禁分层模型、措施矩阵、默认值依据、实现落点、排查对照表 |
| [docs/P2-ACCEPTANCE.md](docs/P2-ACCEPTANCE.md) | P2 稳定性验收演练（断 token 降级、并发闸门、PoW 并发缺陷修复） |
| [CHANGELOG.md](plugins/deepether-proxy/CHANGELOG.md) | 版本更新记录 |

**先看结论**：[docs/FEASIBILITY.md](docs/FEASIBILITY.md) —— 技术可行，但
"反代网页版 = 拿到 DeepSeek 全部最新能力"**不成立**，核心缺口是工具调用（function calling），
而它正是 MaiBot `planner` 的命脉。

## 自动注册（0.2.4）

装好插件、启动 MaiBot 即可，**不用手改 `model_config.toml`**：插件加载时会自动写入

- 服务商 `deepether`（`client_type = com.ether.deepether`、`auth_type = none`、`timeout = 300`）
- 三个模型：`deepether-replyer`（口语化风格、不思考）、`deepether-planner`（开思考）、`deepether-utils`（不思考）
- 把 `deepether-replyer` / `deepether-utils` 接到 `replyer` / `utils` 的 `model_list` 首位，并把
  `selection_strategy` 固定为 `sequential`（只有按顺序优先才会先走网页版）

Host 对 `model_config.toml` 有文件监视 + 热重载，写完立即生效、不用二次重启。
写入前自动备份（最多 5 份），内容无变化时不落盘，避免反复触发热重载。
手动重跑用 `/deepether-setup`；关掉自动写入用插件配置 `[provision] enabled = false`。

> `planner` 默认**不接**是刻意的：网页版没有函数调用，接了只会每次先失败一次再降级。

## 反封禁（0.2.3）

上游账号因"行为特征"被风控是这套方案最大的运行风险（本项目实测被封过 3 天）。0.2.3 起把
"让这个账号看起来像一个人在正常用"做成可配置能力，完整设计见 [docs/ANTIBAN.md](docs/ANTIBAN.md)：

- **节奏**：同 token 串行（并发 1）+ 每次间隔 **8~20 秒随机抖动**，不是固定间隔
- **配额**：3/分 · 30/时 · 200/天 三级滑动窗口，超限直接降级到官方模型（对话不断线）
- **冷却**：429 → 静默 15 分钟；疑似封号 → 6 小时；连续 3 次风控信号升级长冷却，成功一次折半恢复
- **会话复用**：同一上游会话连续用 8 次再换，避免"一堆一句话会话"这种机器特征
- **指纹自洽**：补齐真实浏览器的 `Accept-Language` / `sec-ch-ua*` / `sec-fetch-*`；装了 `h2` 自动走 HTTP/2；
  **不**伪造 `x-client-*` 私有头（取值随前端漂移，写错比不带更可疑）
- **状态可见**：`/deepether-status` 的「反封禁节奏」段落给出间隔、用量、冷却剩余；
  状态落盘到 `runtime_dir/antiban_state.json`，**重启不重置配额、也不重算冷却**

## 仓库结构

```
plugins/deepether-proxy/   插件本体（plugin.py + _manifest.json + deepether/ + tests/）
docs/                      设计与验收文档
scripts/build_package.py   打包脚本：读 _manifest.json 的 version → dist/<name>-<version>.zip
```

## 本地开发与测试

```bash
git clone https://github.com/Eetherrr/DeepEtherProxy.git
cd DeepEtherProxy/plugins/deepether-proxy
pip install maibot-plugin-sdk pytest httpx wasmtime deepseek-pow tomlkit
python -m pytest -q          # 当前：145 passed, 4 skipped（依赖宿主的 2 个用例会自动跳过）
python ../../scripts/build_package.py   # 生成 dist/deepether-proxy-<version>.zip
```

把 `plugins/deepether-proxy` 放进 MaiBot 的 `plugins/` 目录（开发期可用目录联接）即可联调。

## 版本基线

| 组件 | 版本 |
|---|---|
| MaiBot | 1.3.11 |
| maibot-plugin-sdk | 2.12.0 |
| Python | 3.12 |

## 免责声明

通过逆向网页版接口提供模型能力通常违反服务条款，**可能导致账号限流或封禁**。
本项目的反封禁措施只能降低概率，**不能保证账号安全**；本项目仅限个人学习与技术研究，
请自行评估风险。需要稳定合规的模型服务，请使用 [DeepSeek 官方开放平台](https://api-docs.deepseek.com/)。
