<!-- Generated from the Command Code docs: https://commandcode.ai/docs -->

# Command Code Models

The model catalog — every id `/model`, `--model`, `model:effort` shorthand, and agent-file `model:` pinning accept, with context windows, reasoning efforts, and advertised per-1M-token rates. Ids are EXACT — never invent or guess a model id. A BYO-provider id (from a configured custom provider) also works and is passed through as-is.

## Open Source

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `deepseek/deepseek-v4-pro` | DeepSeek V4 Pro (latest) | 1M | off, high, max | $0.66/$1.98 · cache $0.022 | Go and above | hybrid-attention long-context reasoning |
| `deepseek/deepseek-v4-flash` | DeepSeek V4 Flash (latest) | 1M | off, high, max | $0.15/$0.6 · cache $0.003 | Go and above | fast hybrid-attention reasoning |
| `deepseek/deepseek-v4-flash-vision-exp` | DeepSeek V4 Flash Vision (exp) | 1M | off, high, max | $0.15/$0.6 · cache $0.003 | Go and above | fast hybrid-attention reasoning with vision |
| `deepseek/deepseek-v4-flash-fast` | DeepSeek V4 Flash Fast | 1M | low, high, max | $0.28/$0.56 · cache $0.07 | Go and above | low-latency V4 Flash deployment |
| `deepseek/deepseek-v4.1-flash` | DeepSeek V4.1 Flash (latest) | 1M | off, low, high, max | $0.15/$0.6 · cache $0.003 | Go and above | V4.1 hybrid-attention reasoning with vision |
| `deepseek/deepseek-v4.1-flash-fast` | DeepSeek V4.1 Flash Fast (latest) | 1M | off, low, high, max | $0.16/$0.58 · cache $0.016 | Go and above | High throughput V4.1 Flash |
| `moonshotai/Kimi-K3` | Kimi K3 | 1M | low, high, max | $3/$15 · cache $0.3 | Go and above | long-horizon coding & knowledge work with 1M context |
| `moonshotai/Kimi-K2.7-Code` | Kimi K2.7 Code | 256K | — | $0.95/$4 · cache $0.19 | Go and above | improved long-horizon coding with vision |
| `moonshotai/Kimi-K2.7-Code-Highspeed` | Kimi K2.7 Code HighSpeed | 262K | — | $1.9/$8 · cache $0.38 | Go and above | high-speed long-horizon coding with vision |
| `moonshotai/Kimi-K2.6` | Kimi K2.6 | 256K | — | $0.95/$4 · cache $0.16 | Go and above | long-horizon coding with vision |
| `moonshotai/Kimi-K2.5` | Kimi K2.5 | 256K | — | $0.6/$3 · cache $0.1 | Go and above | multimodal frontend coding |
| `z-ai/glm-5.3-flash` | GLM-5.3 Flash | 1.05M | low, high, max | $0.15/$0.5 · cache $0.03 | Go and above | fast, affordable GLM coding with 1M context |
| `z-ai/glm-5.3-flashx` | GLM-5.3 FlashX | 1M | low, high, max | $0.37/$1.25 · cache $0.075 | Go and above | high-speed GLM-5.3 Flash with 1M context |
| `zai-org/GLM-5.3` | GLM-5.3 | 1M | low, high, max | $1.4/$4.4 · cache $0.26 | Go and above | frontier coding with emergent cyber capabilities |
| `zai-org/GLM-5.2` | GLM-5.2 | 1M | high, max | $1.4/$4.4 · cache $0.26 | Go and above | powerful coding with 1M context and long-horizon tasks |
| `zai-org/GLM-5.2-Fast` | GLM-5.2 Fast | 1M | — | $3/$10.25 · cache $0.5 | Go and above | high-throughput GLM-5.2 with 1M context |
| `zai-org/GLM-5.1` | GLM-5.1 | — | — | $1.4/$4.4 · cache $0.26 | Go and above | long-horizon autonomous coding agent |
| `zai-org/GLM-5` | GLM-5 | 200K | — | $1/$3.2 · cache $0.2 | Go and above | multi-mode thinking & long-range planning |
| `minimax/minimax-m3.1-flash` | MiniMax M3.1 Flash | 1M | low, medium, high, xhigh, max | $0.15/$0.6 · cache $0.015 | Go and above | multimodal coding with tunable thinking depth |
| `MiniMaxAI/MiniMax-M3` | MiniMax M3 | 1M | low, medium, high | $0.3/$1.2 · cache $0.06 | Go and above | frontier coding, agents & native multimodality |
| `MiniMaxAI/MiniMax-M2.7` | MiniMax M2.7 | — | — | $0.3/$1.2 · cache $0.06 | Go and above | end-to-end software engineering agent |
| `MiniMaxAI/MiniMax-M2.5` | MiniMax M2.5 | 200K | — | $0.3/$1.2 · cache $0.03 | Go and above | cross-platform full-stack agentic dev |
| `ornith/ornith-1.5-122b` | Ornith 1.5 122B | 262K | — | $0.16/$0.64 · cache $0.02 | Go and above | open-weight MoE for fast agentic coding |
| `xiaomi/mimo-v2.6-pro` | MiMo V2.6 Pro | 1.05M | — | $0.435/$0.87 · cache $0.0036 | Go and above | flagship multimodal agentic coding with 1M context |
| `xiaomi/mimo-v2.6-pro-ultraspeed` | MiMo V2.6 Pro UltraSpeed | 1.05M | — | $4.35/$8.7 · cache $0.036 | GOAT and above | low-latency serving tier of MiMo V2.6 Pro |
| `xiaomi/mimo-v2.6-flash` | MiMo V2.6 Flash | 1.05M | — | $0.14/$0.28 · cache $0.0028 | Go and above | efficient multimodal agentic coding with 1M context |
| `Qwen/Qwen3.8-Omni-Flash` | Qwen 3.8 Omni Flash | 1M | low, medium, xhigh | $0.15/$0.47 · cache $0.016 | Go and above | omni-modal understanding & multimedia agentic work |
| `Qwen/Qwen3.8-Max-Prime` | Qwen 3.8 Max Prime | 1M | low, medium, xhigh | $4/$12 · cache $0.5 (write $5) | GOAT and above | high-speed Qwen 3.8 Max: 1.5–2× faster output |
| `Qwen/Qwen3.8-Max-0902` | Qwen 3.8 Max 0902 | 1M | low, medium, xhigh | $2/$6 · cache $0.25 | Go and above | upgraded Qwen 3.8 Max: stronger coding & agentic tool use |
| `Qwen/Qwen3.8-Max` | Qwen 3.8 Max | 1M | low, medium, xhigh | $2/$6 · cache $0.25 (write $2.5) | Go and above | autonomous long-horizon coding & professional work |
| `Qwen/Qwen3.8-27B` | Qwen 3.8 27B | 262K | low, medium, xhigh | $0.4/$3 · cache $0.04 | Go and above | compact vision-language coding & agentic work |
| `Qwen/Qwen3.8-Flash` | Qwen 3.8 Flash | 1M | low, medium, xhigh | $0.16/$0.47 · cache $0.016 | Go and above | fast low-cost agentic coding & reasoning |
| `Qwen/Qwen3.7-Max` | Qwen 3.7 Max | 1M | — | $2.5/$7.5 · cache $0.5 (write $3.13) | Go and above | frontier coding & long-horizon agent execution |
| `Qwen/Qwen3.7-Plus` | Qwen 3.7 Plus | 1M | — | $0.4/$1.6 · cache $0.08 (write $0.5) | Go and above | agentic coding & reasoning at lower cost |
| `Qwen/Qwen3.7-Flash` | Qwen 3.7 Flash | 1M | — | $0.03/$0.13 · cache $0.006 (write $0.038) | Go and above | fast low-cost agentic coding & reasoning |
| `Qwen/Qwen3.6-Max-Preview` | Qwen 3.6 Max Preview | — | — | $1.3/$7.8 · cache $0.26 (write $1.63) | Go and above | vibe coding & efficient agent execution |
| `Qwen/Qwen3.6-Plus` | Qwen 3.6 Plus | — | — | $0.5/$3 · cache $0.1 | Go and above | agentic coding & reasoning |
| `meituan/LongCat-2.0` | LongCat 2.0 | 1.05M | — | $0.3/$1.2 · cache $0.006 | Go and above | trillion-parameter agentic coding with 1M context |
| `stepfun/Step-5-Preview` | Step 5 Preview | 1M | low, medium, high | $1/$2.7 · cache $0.05 | Go and above | 600B sparse-MoE agentic coding with 1M context |
| `stepfun/Step-3.7-Flash` | Step 3.7 Flash | 256K | — | $0.2/$1.15 · cache $0.04 | Go and above | multimodal sparse-MoE reasoning |
| `stepfun/Step-3.5-Flash` | Step 3.5 Flash | 262K | — | $0.09/$0.3 · cache $0.02 | Go and above | fast sparse-MoE agentic reasoning |
| `tencent/hy3-paid` | Tencent Hy3 | 262K | — | $0.14/$0.58 · cache $0.035 | Go and above | sparse-MoE reasoning & agentic tool use |
| `tencent/hy4-preview` | Tencent Hy4 Preview | 1.05M | low, medium, high | $0.834/$2.501 · cache $0.042 | Go and above | agentic coding & sustained multi-step tool use |
| `nvidia/nemotron-3-ultra-550b-a55b` | Nemotron 3 Ultra | 1M | — | $0.6/$2.4 · cache $0.12 | Go and above | open reasoning model for long-horizon autonomous agents |
| `nvidia/nemotron-3.5-super-vl` | Nemotron 3.5 Super VL | 262K | low, medium, high | $0.2/$0.5 · cache $0.04 | Go and above | open vision-language model for coding and documents |
| `thinkingmachines/inkling` | Inkling | 256K | — | $1/$4.05 · cache $0.17 | Go and above | multimodal MoE reasoning |
| `thinkingmachines/inkling-small` | Inkling Small | 1M | — | $0.5/$1.2 · cache $0.1 | Go and above | lightweight MoE reasoning at lower cost and latency |
| `reflection/beam:free` | Beam 1.0 | 1.05M | low, medium, high, xhigh, max | $0/$0 · cache $0 | Go and above | reasoning model for coding and agentic workflows |
| `poolside/laguna-s-2.1-free` | Laguna S 2.1 | 256K | — | $0/$0 · cache $0 | Go and above | open-weight agentic coding and long-horizon work |
| `inclusionai/ling-3.1-flash` | Ling 3.1 Flash | 262K | low, medium, high | $0.3/$0.9 · cache $0.06 | Go and above | hybrid-reasoning MoE for coding & tool-using agents |
| `mistral/mistral-large-4` | Mistral Large 4 | 524K | low, medium, high | $1.36/$4.18 · cache $0.14 | Go and above | frontier open-weight multimodal model for coding & analysis |
## Stealth

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `stealth/glyph-cluster:free` | Glyph Cluster | 256K | low, medium, high, xhigh | $0/$0 · cache $0 | Go and above | stealth reasoning model in early access |
## Anthropic

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `claude-sonnet-5-5` | Claude Sonnet 5.5 | 1M | low, medium, high, xhigh, max | $2/$10 · cache $0.1 (write $2.5) | GOAT and above | best combo of speed & intelligence (recommended) |
| `claude-sonnet-5` | Claude Sonnet 5 | 1M | low, medium, high, xhigh, max | $2/$10 · cache $0.2 (write $2.5) | Pro and above | prev Sonnet, still fast & capable |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 | 1M | low, medium, high, xhigh, max | $3/$15 · cache $0.3 (write $3.75) | Pro and above | older Sonnet, still fast & capable |
| `claude-fable-5-1` | Claude Fable 5.1 | 1M | low, medium, high, xhigh, max | $10/$50 · cache $0.25 (write $12.5) | Max | most capable for demanding reasoning & long-horizon agents |
| `claude-fable-5` | Claude Fable 5 | 1M | low, medium, high, xhigh, max | $10/$50 · cache $1 (write $12.5) | Max | prev Fable, still strong for deep reasoning & agents |
| `claude-opus-5-5` | Claude Opus 5.5 | 1M | low, medium, high, xhigh, max | $4/$20 · cache $0.2 (write $5) | Max | most intelligent Opus for agents and coding |
| `claude-opus-5` | Claude Opus 5 | 1M | low, medium, high, xhigh, max | $5/$25 · cache $0.5 (write $6.25) | Max | prev flagship, still strong for agents and coding |
| `claude-opus-4-8` | Claude Opus 4.8 | 1M | low, medium, high, xhigh, max | $5/$25 · cache $0.5 (write $6.25) | Max | older Opus, still strong for agents and coding |
| `claude-opus-4-7` | Claude Opus 4.7 | 1M | low, medium, high, xhigh, max | $5/$25 · cache $0.5 (write $6.25) | Max | older Opus, still strong for agents and coding |
| `claude-haiku-5-5` | Claude Haiku 5.5 | 1M | low, medium, high, xhigh, max | $0.1/$0.5 · cache $0.01 (write $0.125) | GOAT and above | fastest & most compact, great for quick tasks |
| `claude-haiku-4-5-20251001` | Claude Haiku 4.5 | 200K | — | $1/$5 · cache $0.1 (write $1.25) | Pro and above | prev Haiku, still fast for quick tasks |
## OpenAI

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `gpt-6-astra` | GPT-6 Astra | 1.05M | low, medium, high, xhigh, max | $10/$50 · cache $1 (write $12.5) | Max | most capable OpenAI model for demanding reasoning & agents |
| `gpt-6.1-sol` | GPT-6.1 Sol | 1.05M | low, medium, high, xhigh, max | $2/$10 · cache $0.1 (write $2.5) | Max | near-Astra performance for complex work at a lower cost |
| `gpt-6-sol` | GPT-6 Sol | 1.05M | low, medium, high, xhigh, max | $2/$10 · cache $0.2 (write $2.5) | Pro and above | built for complex coding & agentic workflows |
| `gpt-6-luna` | GPT-6 Luna | 1.05M | low, medium, high, xhigh, max | $0.1/$0.5 · cache $0.01 (write $0.125) | Go and above | most efficient OpenAI model for focused, high-volume tasks |
| `gpt-5.6-sol` | GPT-5.6 Sol | 1.05M | low, medium, high, xhigh, max | $5/$30 · cache $0.5 (write $6.25) | GOAT and above | frontier model for complex professional work |
| `gpt-5.6-terra` | GPT-5.6 Terra | 1.05M | low, medium, high, xhigh, max | $2/$12 · cache $0.2 (write $2.5) | Pro and above | balances intelligence and cost |
| `gpt-5.6-luna` | GPT-5.6 Luna | 1.05M | low, medium, high, xhigh, max | $0.2/$1.2 · cache $0.02 (write $0.25) | Go and above | optimized for cost-sensitive workloads |
| `gpt-5.5` | GPT-5.5 | 400K | low, medium, high, xhigh | $5/$30 · cache $0.5 (write $0) | Pro and above | latest frontier model for general complex work |
| `gpt-5.4` | GPT-5.4 | 400K | low, medium, high, xhigh | $2.5/$15 · cache $0.25 (write $0) | Pro and above | frontier model for general complex work |
| `gpt-5.3-codex` | GPT-5.3 Codex | 400K | low, medium, high, xhigh | $2/$8 · cache $0.5 (write $0) | Pro and above | frontier coding model |
| `gpt-5.4-mini` | GPT-5.4 Mini | 400K | low, medium, high | $0.75/$4.5 · cache $0.075 (write $0) | Pro and above | fast, cost-effective model for everyday tasks |
## Google

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `google/gemini-4-argon` | Gemini 4 Argon | 1M | low, medium, high | $2/$10 · cache $0.1 | Pro and above | multimodal reasoning for documents and agent workflows |
| `google/gemini-3.8-flash` | Gemini 3.8 Flash | 1M | low, medium, high | $1.5/$7.5 · cache $0.15 | GOAT and above | newest Gemini Flash, improved core reasoning |
| `google/gemini-3.7-flash` | Gemini 3.7 Flash | 1.05M | low, medium, high | $1.5/$7.5 · cache $0.15 (write $0.08334) | GOAT and above | higher-quality coding & agentic workflows, fewer tokens |
| `google/gemini-3.6-flash` | Gemini 3.6 Flash | 1M | low, medium, high | $1.5/$7.5 · cache $0.15 | Pro and above | previous Gemini Flash, still fast & capable |
| `google/gemini-3.5-flash` | Gemini 3.5 Flash | 1M | low, medium, high | $1.5/$9 · cache $0.15 | Pro and above | Pro-level coding proficiency, parallel agentic execution |
| `google/gemini-3.5-flash-lite` | Gemini 3.5 Flash Lite | 1M | low, medium, high | $0.3/$2.5 · cache $0.03 | Pro and above | upgraded agentic capabilities, ideal for subagents |
| `google/gemini-3.1-flash-lite` | Gemini 3.1 Flash Lite | 1M | low, medium, high | $0.25/$1.5 · cache $0.03 | Pro and above | high-volume workhorse model with implicit caching |
## Sakana

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `sakana/fugu-ultra` | Fugu Ultra | 1M | high, xhigh | $5/$30 · cache $0.5 | Max | multi-agent orchestration across frontier models |
## Meta

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `meta/muse-spark-1.1` | Muse Spark 1.1 | 1.05M | low, medium, high, xhigh | $1.25/$4.25 · cache $0.15 | Pro and above | agentic performance, tool use, and computer use |
| `meta/muse-spark-1.2` | Muse Spark 1.2 | 1.05M | low, medium, high, xhigh | $1.25/$4.25 · cache $0.15 | GOAT and above | coding-optimized for agentic workflows and large codebases |
| `meta/muse-spark-1.2-contributor` | Muse Spark 1.2 Contributor | 1.05M | low, medium, high, xhigh | $0.1/$0.2 · cache $0.002 | Go and above | Muse Spark 1.2 at ~95% off |
| `meta/muse-spark-1.3` | Muse Spark 1.3 | 1.05M | low, medium, high, xhigh, max | $1.25/$4.25 · cache $0.15 | GOAT and above | multimodal reasoning for long-horizon agentic and coding workflows |
| `meta/muse-spark-1.3-contributor` | Muse Spark 1.3 Contributor | 1.05M | low, medium, high, xhigh | $0.1/$0.2 · cache $0.002 | Go and above | Muse Spark 1.3 at up to 95% off |
| `meta/muse-spark-open` | Muse Spark Open | 1.05M | low, medium, high | $1.25/$4.25 · cache $0.15 | Go and above | open-weight Muse Spark for coding and agentic workflows |
| `meta/muse-wonder-1.0` | Muse Wonder | 1.05M | low, medium, high, xhigh | $4/$20 · cache $0.2 | Max | frontier coding, long-running agents and advanced computer use |
## xAI

| Id (use EXACTLY this) | Name | Context | Efforts | $/1M in/out · cache read | Min plan | Best for |
|---|---|---|---|---|---|---|
| `xai/grok-4.5` | Grok 4.5 | 500K | low, medium, high | $2/$6 · cache $0.5 | Go and above | smartest model for coding, agentic tasks, knowledge work |
| `xai/grok-4.6` | Grok 4.6 | 500K | low, medium, high, xhigh | $2/$6 · cache $0.5 | GOAT and above | frontier performance on coding, knowledge work, and STEM |
| `xai/grok-4.7` | Grok 4.7 | 500K | low, medium, high, xhigh | $2/$6 · cache $0.5 | GOAT and above | coding and knowledge work, built for multi-hour tasks |

Rates are the advertised price list resolved from the billing source of truth (promos are already baked in): input / output / cache-read per 1M tokens, plus cache-write where the provider bills it. Min plan is the cheapest public plan that serves the model, ordered Go < GOAT < Pro < Max (see plans.md). Models without an effort column entry decide their own reasoning depth. Full pricing detail: https://commandcode.ai/docs/resources/pricing-limits
