# DeepEtherProxy

把 **DeepSeek 网页版**反代为 **MaiBot** 的模型能力（MaiBot 第三方插件）。

- 形态：`plugins/deepether-proxy/`，通过 `@LLMProvider` 注册 `client_type`，**不修改 MaiBot 主程序**
- 版本：**v0.2.4**（P0 骨架 / P0.5 凭证 / P1 端到端 / P2 稳定性 / 联网搜索实测 / P2.5 反封禁 / **P2.6 自动注册模型** 均已交付）
- 一期范围：**只接 `replyer` + `utils`**（`planner` 强依赖工具调用，不接）

| 文档 | 内容 |
|---|---|
| [plugins/deepether-proxy/README.md](plugins/deepether-proxy/README.md) | **安装、授权、配置、排查（用户向）** |
| [plugins/deepether-proxy/docs/upstream-protocol.md](plugins/deepether-proxy/docs/upstream-protocol.md) | 网页版协议实测记录（含与文档的差异） |
| [docs/P2-ACCEPTANCE.md](docs/P2-ACCEPTANCE.md) | P2 稳定性验收演练（断 token 降级 + 并发 20 闸门） |
| [docs/ANTIBAN.md](docs/ANTIBAN.md) | **反封禁设计**（封禁分层模型、措施矩阵、默认值依据、实现落点、排查对照表） |
| [docs/FEASIBILITY.md](docs/FEASIBILITY.md) | 可行性分析报告（决策依据） |

## 先读这个

👉 **[docs/FEASIBILITY.md](docs/FEASIBILITY.md)** —— 可行性分析报告（结论、协议核实、能力差距、推荐架构、实施路线、测试方案）

一句话结论：**技术可行，但"反代网页版 = 拿到 DeepSeek 全部最新能力"不成立**。核心缺口是工具调用（function calling），
而它正是 MaiBot `planner` 的命脉。

### 已确认的方向（2026-10-20）

| 决策 | 内容 |
|---|---|
| 默认通道 | **网页版优先**，官方 API 兜底 |
| 目的 | **省群聊 token**（一期把 `replyer` + `utils` 迁过去） |
| 凭证 | **手动从浏览器控制台复制 token，填进插件配置**（WebUI 可改）；开发期另有可选的本地授权页/书签 |
| 范围 | 仅个人使用 |
| 硬要求 | **长期稳定** → 凭证自愈 + 自动降级 + 降级可见是必需项 |

一期不碰 `planner`（它强依赖工具调用）；等工具调用模拟层的回归准确率 ≥95% 再谈迁移，见报告 §4.4 与 §7。

**起步范围：只接 `replyer` 一个任务**（已核实可行，见报告 §4.5）——它不需要工具调用、不带图片，
且 `model_list` 自带主备切换（`["deepehter-web", "deepseek-official"]`）。跑稳后再扩 `utils` / `mid_memory`。

## 目录

| 路径 | 说明 |
|---|---|
| `docs/FEASIBILITY.md` | 可行性分析报告（主交付物） |
| `docs/P2-ACCEPTANCE.md` | P2 稳定性验收演练报告（断 token 降级、并发闸门） |
| `docs/ANTIBAN.md` | 反封禁设计与实现落点（0.2.3） |
| `dist/deepether-proxy-<version>.zip` | 可手动安装的插件包（`python scripts/build_package.py` 生成） |
| `scripts/build_package.py` | 打包脚本（读 `_manifest.json` 的 version 定名，排除 tests/缓存） |
| `docs/_ref/maibot/` | MaiBot 官方文档离线快照，供引用核对 |
| `.testenv/maibot/` | **工作区内的隔离测试实例**（MaiBot 1.3.11），不进版本库；WebUI 41082 |

## 自动注册（0.2.4）

装好插件、启动 MaiBot 即可，**不用再手改 `model_config.toml`**：插件加载时会自动写入服务商
`deepether` 与三个模型（`deepether-replyer` / `deepether-planner` / `deepether-utils`），
并把 `replyer` + `utils` 接到 `sequential` 主备列表的首位。Host 对 `model_config.toml` 有文件监视 +
热重载，写完立即生效、不用重启；写入前自动备份、内容无变化时不落盘（避免反复热重载）。

- 手动重跑：`/deepether-setup`；看结果：`/deepether-status` 的「自动注册」段落
- 关掉自动写入：插件配置 `[provision] enabled = false`
- 没接 `planner` 是故意的：它强依赖工具调用，网页版没有函数调用，接了只会每次先失败一次再降级

## 反封禁（0.2.3）

上游网页版账号因"行为特征"被风控是这套方案最大的运行风险（本项目实测已被封 3 天）。0.2.3 起把
"让这个账号看起来像一个人在正常用"做成可配置能力，完整设计见 **[docs/ANTIBAN.md](docs/ANTIBAN.md)**：

- **节奏**：同 token 串行（并发 1）+ 每次间隔 **8~20 秒随机抖动**，不是固定间隔
- **配额**：3/分 · 30/时 · 200/天 三级滑动窗口，超限直接降级到 Host 官方模型（对话不断线）
- **冷却**：429 → 静默 15 分钟；疑似封号 → 6 小时；连续 3 次风控信号自动升级长冷却，成功一次折半恢复
- **会话复用**：同一上游会话连续用 8 次再换，避免"一堆一句话会话"这种机器特征
- **指纹自洽**：补齐真实浏览器的 `Accept-Language` / `sec-ch-ua*` / `sec-fetch-*`；装了 `h2` 就自动走 HTTP/2；
  **不**伪造 `x-client-*` 私有头（取值随前端漂移，写错比不带更可疑）
- **状态可见**：`/deepether-status` 的「反封禁节奏」段落给出间隔、用量、冷却剩余；节奏状态落盘到
  `runtime_dir/antiban_state.json`，**重启不重置配额、也不重算冷却**（"重启不加刑"）

## 测试实例（不碰日用实例）

工作区内已部署一份独立的 MaiBot 副本用于插件联调，**不会触碰你正在使用的那份**：

- 路径：`.testenv/maibot`，WebUI：http://127.0.0.1:41082
- NapCat 适配器已停用，测试期不会登录 QQ
- 启动：`Start-Process .testenv/maibot/.venv/Scripts/python.exe -ArgumentList bot.py -WorkingDirectory .testenv/maibot`
- ⚠️ **不要重定向 stdin**：MaiBot 读终端输入，stdin 一旦 EOF 会立即优雅关闭
- 日志：`.testenv/maibot/logs/app_*.log.jsonl`

## 核实基线

| 组件 | 版本 |
|---|---|
| MaiBot | 1.3.11 |
| maibot-plugin-sdk | 2.12.0 |

## 免责声明

通过逆向网页版接口提供模型能力，通常违反服务条款，可能导致账号限流或封禁。
本项目仅限个人学习与技术研究，请自行评估风险；需要稳定合规的模型服务请使用 DeepSeek 官方开放平台。
