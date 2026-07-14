# heige-image

<div align="center">

![Version](https://img.shields.io/badge/version-1.0.0-F5A8B8)
![Claude Skill](https://img.shields.io/badge/Claude-Skill-FBF3EC)
![License](https://img.shields.io/badge/license-MIT-blue)

**黑哥设计生图 | Route by image type, design before you draw**

一颗设计脑，按图类型选引擎，把大白话需求翻成结构化规格再出图。

[这是什么](#这是什么-what-is-this) · [为什么不一样](#为什么不一样) · [安装](#1️⃣-安装) · [配置-api](#2️⃣-配置-api) · [怎么用](#3️⃣-怎么用) · [English](#english) · [致谢](#致谢-credits) · [许可证](#许可证-license)

</div>

---

## 这是什么 What is this

heige-image 是一个中文设计生图 skill，给 Claude Code 这类 agent 用。核心思路是先想清楚要什么图，再选对引擎，最后出图，中间多了一颗设计脑，出来的图改图不漂移。

- ✅ **按图类型路由出图**，不按 key 也不按平台分，先认载体和文字层级，再决定走哪条引擎。
- ✅ **插画走粘土 3D 卡通，走 API 引擎**，编译可复现的英文规格再出图，配画面叙事的治愈插画和概念图。
- ✅ **版式走 guizang 简约，走免费 HTML 渲染**，封面和信息图这类文字要逐字精确的图用浏览器截图，零 API、不烧额度、中文一个像素不差。
- ✅ **设计脑三步走**，需求翻译成 6 维 brief，查决策矩阵填有理由的默认，按 8 要点编译结构化 prompt，缺维度不反问。
- ✅ **规格文件可复现**，每张图先落 `prompts/NN-主题.md` 记下 brief、设计决策、最终 prompt、invariant 清单，改图只动单变量，其余逐条复述。

一句话定位：要逐字精确的版式图走免费的 HTML 渲染，要画面叙事的治愈插画走 API 引擎，两条引擎各干各擅长的。

## 为什么不一样

| 维度 | 裸调生图 API | heige-image |
|------|------------|------------|
| 出图前 | 把提示词直接丢给模型 | 先翻 6 维 brief，查矩阵填默认，编译结构化规格 |
| 引擎 | 一条路全包，文字长了就崩 | 按文字层级分流，精确版式走免费 HTML 渲染，画面叙事走 API |
| 风格 | 每次随机，认不出是谁画的 | 两套定死的视觉签名，盖掉标题也认得出是一家 |
| 改图 | 整段重写提示词，越改越漂 | 只动 invariant 里的单一变量，其余逐条锁死 |
| 文字 | 中文标题大概率拼错糊字 | 要精确文字交给浏览器渲染，字焊在版面上不崩 |
| 成本 | 张数无上限，容易烧额度 | 单次张数护栏 + 回炉最多 1 轮，版式引擎全程免费 |

## 1️⃣ 安装

把仓库 clone 到你的 skill 目录，目录名保持 `heige-image`（skill 名要和目录名一致）。

Claude Code 的用户级 skill 目录是 `~/.claude/skills/`：

```bash
git clone https://github.com/HeiGeAi/heige-image.git ~/.claude/skills/heige-image
```

装好后新开一个会话，跟 agent 说「生图」「给文章配图」「画个治愈风的图」就会触发。

两条引擎各自要装的依赖，按需装：

```bash
# 版式渲染引擎 render.py（免费、不要 key）需要 Playwright
python3 -m pip install playwright && python3 -m playwright install chromium

# API 插画引擎 gen.py 需要 httpx
python3 -m pip install httpx
```

只想用免费版式图，装 Playwright 就够，连 API key 都不用配。

## 2️⃣ 配置 API

只有 API 插画引擎（`scripts/gen.py`）要配 key，版式渲染引擎（`scripts/render.py`）零 API、免费，跳过这一步也能用。

**接口走固定的 OpenAI 图像生成请求契约，你自己配 `base_url` + `api_key` + `model` 三项。** 当前支持 OpenAI 官方，或遵循同一套 Bearer 认证与 `/images/generations` 路径契约的兼容渠道。Azure 当前未原生适配，因为 Azure 通常要求 `api-key`、deployment 路径和 `api-version`。三项配置三种方式，优先级从高到低：命令行 > 环境变量 > 配置文件 > 默认值。

**API 引擎的文件契约是静态 PNG，不支持 APNG。** 输出路径必须以 `.png` 结尾。脚本会在落盘前校验 PNG 签名、chunk CRC、完整像素流和 50 MiB 体积上限；返回 APNG、JPEG、WebP、HTML 错误页、截断文件或超大数据的兼容渠道会被明确拒绝。

| 配置项 | 命令行 | 环境变量 | 默认值 |
|--------|--------|----------|--------|
| API 基址 | `--base-url` | `HEIGE_IMAGE_BASE_URL` | `https://api.openai.com/v1` |
| API Key | `--api-key` | `HEIGE_IMAGE_API_KEY` | 无，必填一项 |
| 模型 | `--model` | `HEIGE_IMAGE_MODEL` | `gpt-image-2` |

最省心的是写一个配置文件 `~/.heige-image/config.json`：

```json
{"base_url": "https://api.openai.com/v1", "api_key": "sk-xxx", "model": "gpt-image-2"}
```

接 OpenAI 官方就用上面这份。兼容渠道仅限使用 Bearer 认证，并以 `base_url + /images/generations` 接收同形请求、返回同形响应的渠道；模型名按渠道实际支持的值填写。

> 国内想直接出图、不想自己折腾通道，可以评估 gptx.cc。使用前仍要确认它当前符合 Bearer 认证与 `/images/generations` 固定契约；base_url 填 `https://api.gptx.cc/v1`，并使用你自己的 key。这只是推荐，不写死也不强制。

## 3️⃣ 怎么用

日常你只要跟 agent 说人话，设计脑三步和引擎分流都自动走。下面是底层脚本的直接调法，方便排查和单独跑。

**版式图（封面 / 信息图 / 卡片，文字要逐字精确，走免费 HTML 渲染）**

选 `templates/` 下的模板，把里面的 `{{变量}}` 手填成真内容，再截图：

```bash
# 截某个节点，指定输出
python3 scripts/render.py templates/cover-clean.html --node "#cover" -o outimage/01-封面.png

# 不指定节点，抓页面里所有 section.poster 逐张出
python3 scripts/render.py templates/infographic-clean.html -o outimage/
```

字是 HTML 文本，浏览器拿系统字体渲染，断网也不出豆腐块，中文一个像素不差。

**插画 / 概念图（画面叙事，走 API 引擎，出粘土 3D 卡通）**

先落规格文件 `prompts/NN-主题.md`，再读规格出图（主推，规格即真相）：

```bash
python3 scripts/gen.py --spec prompts/01-粘土小机器人章节图.md -ar 16:9
```

临时直给提示词（不落规格，不推荐常用）：

```bash
python3 scripts/gen.py --prompt "完整英文 prompt" -ar 16:9 -o outimage/test.png
```

并发批量出一组配图，张数超 `--max-n`（默认 6）会被成本护栏拦：

```bash
python3 scripts/gen.py --batch tasks.json --workers 2
```

默认自动重试为 0，因为生图 POST 无幂等承诺。只有显式传入 `--retry N` 才会重试；超时时上游可能已经生成并计费，再试可能重复生成、重复计费。`--max-n` 只限制目标张数；显式重试会增加实际请求次数，不受该张数上限代替约束。

`tasks.json` 中的所有 `output` 也必须是 `.png`。启动请求前，脚本会对绝对路径、`..` 和符号链接父目录做归一化，拒绝任何会覆盖同一文件的重复任务。

本脚本为兼容不同渠道，固定只开放三个尺寸桶：横→1536×1024、竖→1024×1536、方→1024×1024。比例参数是构图意图，不代表模型本身只支持这三档。

---

## English

heige-image is a Chinese-first design image generation skill for agents like Claude Code. It adds a design brain before drawing: translate a plain-language request into a six-dimension brief, fill defaults from a decision matrix, compile a reproducible prompt spec, then generate. The point is consistency and no drift when you re-edit.

Two engines, routed by image type rather than by API key:

- **Layout images** (covers, infographics, cards where text must be pixel-perfect) go through a free, zero-API HTML rendering engine (`scripts/render.py`). HTML and CSS weld the text onto the page, Playwright screenshots it. Free, no quota burn, Chinese text never breaks.
- **Illustrations and concept art** (visual storytelling, little text) go through the API engine (`scripts/gen.py`), which compiles a reproducible English prompt spec and sends it to OpenAI official or a compatible provider that implements the same Bearer-authenticated `/images/generations` contract.

**The API engine uses one fixed request contract.** You configure `base_url` + `api_key` + `model` yourself (CLI > env var > config file > default). It supports OpenAI official and compatible relays that use Bearer authentication with the `/images/generations` path. Azure-specific `api-key`, deployment routing, and `api-version` are not natively adapted. Config file at `~/.heige-image/config.json`:

```json
{"base_url": "https://api.openai.com/v1", "api_key": "sk-xxx", "model": "gpt-image-2"}
```

The API engine has a static-PNG-only output contract and rejects APNG. It also rejects non-`.png` paths, duplicate normalized batch destinations, non-PNG or truncated responses, and payloads larger than 50 MiB before writing a file.

If you are in mainland China, you may evaluate gptx.cc. Before use, verify that its current API still follows the Bearer-authenticated `/images/generations` contract; if it does, set `base_url` to `https://api.gptx.cc/v1` and use your own key. It is a suggestion, not hardcoded or required.

Install by cloning into your Claude Code skill directory:

```bash
git clone https://github.com/HeiGeAi/heige-image.git ~/.claude/skills/heige-image
python3 -m pip install playwright && python3 -m playwright install chromium   # for render.py
python3 -m pip install httpx                                                # for gen.py
```

The free layout engine needs only Playwright, no API key at all.

## 致谢 Credits

- 无 key 版式渲染引擎（`render.py` + `templates/*-clean.html`）的种子 HTML + Playwright 截图机制，受 guizang 的瑞士 / 编辑杂志简约风启发，已踩的无头模式 SVG 滤镜空白、字体兜底等坑焊进了实现。
- 生图走固定的 Bearer 认证与 `/images/generations` 请求契约，模型与符合该契约的具体渠道由使用者自行配置。

## 许可证 License

MIT，Copyright (c) 2026 HeiGeAi (Blake Xu)。详见 [LICENSE](./LICENSE)。

## 更多开源工具

本项目属于黑哥 AI 的开源武器库。全部开源项目的清单、用途和协议,见 [heigeai.com/opensource](https://www.heigeai.com/opensource/)。
