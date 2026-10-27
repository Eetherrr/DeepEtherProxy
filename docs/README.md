# 文档索引

> 最后更新：2026-10-27　适用版本：0.5.0
> 这份文档只回答一件事：**要做什么事，该看哪份文件**。

## 0. 三个入口

| 你想做的事 | 从这里开始 |
|---|---|
| 安装、授权、日常使用 | [plugins/deepether-proxy/README.md](../plugins/deepether-proxy/README.md) |
| 改代码 / 加功能 / 调参数 | **[SKILL.md](SKILL.md)**（项目规范，改之前必读） |
| 查某个坑 / 某个决定为什么这么做 | [pitfalls/PITFALLS.md](pitfalls/PITFALLS.md)、[records/DECISIONS.md](records/DECISIONS.md) |

## 1. 目录结构

```
docs/
├─ README.md                      ← 本文件：索引 + 维护规则
├─ SKILL.md                       ← 项目规范（红线、代码地图、流程、参数依据、验收）
├─ FEASIBILITY.md                 ← 总纲：可行性结论、架构取舍、里程碑（历史版本多，看最新修订行）
├─ design/                        ← 现行设计（写代码前以这里为准）
│  ├─ ANTIBAN.md                  ← 反封禁：分层模型、措施矩阵、参数依据、观测
│  └─ TASK-SUITABILITY.md         ← 哪些模型任务能接网页版、哪些永远别接
├─ records/                       ← 记录（结论与验收，不再改结论）
│  ├─ DECISIONS.md                ← 决策日志（含被否决的方案与理由）
│  └─ P2-ACCEPTANCE.md            ← 0.2.x 的稳定性验收演练（历史）
├─ pitfalls/
│  └─ PITFALLS.md                 ← 踩坑库：现象→根因→证据→修法→回归
├─ research/                      ← 专题研究（可能已过时，先看顶部「状态」行）
│  ├─ COMMANDCODE-FEASIBILITY.md  ← CommandCode 通道可行性
│  ├─ session-reuse.md            ← 会话上下文复用（已否决，留档）
│  ├─ OPTIMIZATION-FEASIBILITY.md ← 三项优化需求（额度切换/会话/账号池）的可行性
│  └─ commandcode/                ← 协议实证、P0/P1 实测、模型目录快照
└─ _ref/maibot/                   ← 宿主文档离线快照（外部资料，不要改）
```

## 2. 维护规则（新需求 / 改功能 / 优化都必须照做）

1. **先研究后动手**：新需求先在 `research/` 写一页可行性（问题、约束、选项、证据、结论、工作量）；
2. **先决策后实现**：结论写进 [records/DECISIONS.md](records/DECISIONS.md)（包含「否决了什么、为什么」）；
3. **实现时同步设计文档**：改参数/机制就改 `design/` 对应章节，别让文档和代码各说各话；
4. **验收留档**：演练/压测/真机验证写进 `records/`，带日期、版本、可复现命令；
5. **踩过的坑必须进坑库**：哪怕只是「测试在凌晨会红」这种小事；
6. **被否决的方案不删除**：在决策日志里保留，研究稿顶部标「已否决」。

每份文档顶部都要有三行：**状态 / 最后更新 / 适用版本**。

## 3. 三套模板

**可行性（research/）**

```
## 背景与问题
## 约束（不能改什么）
## 选项对比（表格：可行性/代价/风险）
## 证据（file:line、日志样本、实测结论）
## 结论与推荐
## 工作量
```

**决策（records/DECISIONS.md）**

```
### D-00x 标题
- 日期 / 状态（采纳 / 否决 / 待定）
- 背景：
- 选项：
- 决定：
- 影响与代价：
- 证据：
- 回滚方式：
```

**踩坑（pitfalls/PITFALLS.md）**

```
### P-xx 一句话标题
- 现象：
- 根因：
- 证据：file:line 或日志样本
- 修法：
- 回归：哪条测试/演练挡住了
- 状态：已修复（版本）/ 未修复 / 已规避
```

## 4. 现状速查

| 项 | 现状 |
|---|---|
| 版本 | 0.5.0（0.3.2 容量模型重构、0.4.0 CommandCode 通道、0.5.0 注册-Only + replyer 让位） |
| Provider | `com.ether.deepether`（DeepSeek 网页版反代）/ `com.ether.commandcode`（CLI 网关） |
| 自动注册 | **只注册模型、不接线**（0.5.0）：任务用哪个模型由用户在 WebUI 选；`planner` / `emoji` 永不注册 |
| CommandCode | 配置**只有 `api_key`**；没 Key 不注册模型；预算/并发等为代码常量 |
| 额度让位 | replyer 额度用完时由 hook 改走官方模型（0.5.0），不再产生 error 日志 |
| 反封禁 | 串行 + 8~20s 抖动 + 3/分 · 45/时 · 400/天 + AIMD + 夜间节律 + 按模型配额 + 额度保留 |
| 会话 | **只复用 chat_session_id（20 次），不做上下文复用**（2026-10-27 决定） |
| 已知限制 | 见 [SKILL.md](SKILL.md) §9 与各研究文档的「已知缺口」 |
