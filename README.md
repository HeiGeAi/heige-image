# heige-image

<div align="center">

![Version](https://img.shields.io/badge/version-2.0.0-0F766E)
![Claude Skill](https://img.shields.io/badge/Claude-Skill-F6ECE4)
![License](https://img.shields.io/badge/license-MIT-blue)

**需求驱动设计生图 | Demand-driven, with a built-in design director**

用户说了要什么就照做；没说的，内置设计总监根据配套文件推断出最合适的画风、配色、场景，每个决策带理由。

[这是什么](#这是什么-what-is-this) · [为什么不一样](#为什么不一样) · [安装](#1️⃣-安装) · [配置-api](#2️⃣-配置-api) · [怎么用](#3️⃣-怎么用) · [English](#english) · [致谢](#致谢-credits) · [许可证](#许可证-license)

</div>

---

## 这是什么 What is this

heige-image 是一个中文设计生图 skill，给 Claude Code 这类 agent 用。核心思路是先想清楚要什么图，让内置的设计总监定方案，再选对引擎出图。没有写死的画风预设，风格跟着这一次的需求走。

- ✅ **用户需求最大**：点名的画风、配色、场景、比例一律照办，设计总监只补缺口，无权改判。
- ✅ **没指定就推断**：设计总监从配套文件（要配图的文章、品牌规范、参考图、同系列已有图）提取主题、情绪、既有色彩，产出一份每项带理由的《设计方案》，缺维度也不反问。
- ✅ **按图类型路由引擎**：文字要逐字精确的版式图（封面/信息图/卡片）走免费 HTML 渲染，浏览器截图、零 API、中文一个像素不差；画面叙事的插画概念图走 API 引擎；改已有的图走图生图引擎。
- ✅ **风格库当货架**：十个风格词条（软萌 3D、扁平插画、等距 2.5D、水彩、编辑简约、胶片写实、复古印刷、暗色科技、水墨、像素）备好 prompt 词汇和禁忌项，设计总监按需选用，用户点名库外风格也照做。
- ✅ **规格文件可复现**：每张走 API 引擎的图先落 `prompts/NN-主题.md` 记下 brief、设计方案、最终 prompt、invariant 清单，改图只动单变量，其余逐条锁死不漂移；版式图的真相落在填好的 HTML 里。

一句话定位：要逐字精确的版式图走免费的 HTML 渲染，要画面叙事的插画走 API 引擎；用户说了算，没说的设计总监按配套文件推断。

## 为什么不一样

| 维度 | 裸调生图 API | heige-image |
|------|------------|------------|
| 出图前 | 把提示词直接丢给模型 | 设计总监先出《设计方案》：画风、色板、场景每项带理由 |
| 风格 | 每次随机，模型自由发挥 | 用户指定 > 配套文件推断 > 风格库默认，三级决策链逐项可查 |
| 引擎 | 一条路全包，文字长了就崩 | 按文字层级分流，精确版式走免费 HTML 渲染，画面叙事走 API |
| 改图 | 整段重写提示词，越改越漂 | 只动 invariant 里的单一变量，其余逐条锁死 |
| 文字 | 中文标题大概率拼错糊字 | 要精确文字交给浏览器渲染，字焊在版面上不崩 |
| 验收 | 出完就算完 | 设计总监按方案逐项验收 + 反 AI 体检，回炉最多 1 轮 |
| 成本 | 张数无上限，容易烧额度 | 单次张数护栏 + 回炉上限，版式引擎全程免费 |

## 1️⃣ 安装

把仓库 clone 到你的 skill 目录，目录名保持 `heige-image`（skill 名要和目录名一致）。

Claude Code 的用户级 skill 目录是 `~/.claude/skills/`：

```bash
git clone https://github.com/HeiGeAi/heige-image.git ~/.claude/skills/heige-image
```

装好后新开一个会话，跟 agent 说「生图」「给文章配图」「画个某某风格的图」就会触发。

三条引擎各自要装的依赖，按需装：

```bash
# 版式渲染引擎 render.py（免费、不要 key）需要 Playwright
pip install -r requirements.txt && playwright install chromium

# API 引擎 gen.py（文生图）和 edit.py（图生图）需要 httpx
pip install -r requirements.txt
```

只想用免费版式图，装 Playwright 就够，连 API key 都不用配。

## 2️⃣ 配置 API

只有 API 引擎（`scripts/gen.py` / `scripts/edit.py`）要配 key，版式渲染引擎（`scripts/render.py`）零 API、免费，跳过这一步也能用。

**接口走 OpenAI 兼容的图像生成协议，你自己配 `base_url` + `api_key` + `model` 三项。** 官方 OpenAI、Azure、各家中转都能接，填自己那把就行，不写死任何渠道。三项配置三种方式，优先级从高到低：命令行 > 环境变量 > 配置文件 > 默认值。

| 配置项 | 命令行 | 环境变量 | 默认值 |
|--------|--------|----------|--------|
| API 基址 | `--base-url` | `HEIGE_IMAGE_BASE_URL` | `https://api.openai.com/v1` |
| API Key | `--api-key` | `HEIGE_IMAGE_API_KEY` | 无，必填一项 |
| 模型 | `--model` | `HEIGE_IMAGE_MODEL` | `gpt-image-2` |

最省心的是写一个配置文件 `~/.heige-image/config.json`：

```json
{"base_url": "https://api.openai.com/v1", "api_key": "sk-xxx", "model": "gpt-image-2"}
```

接 OpenAI 官方就用上面这份，base_url 换成你的中转地址就接中转，模型名按你渠道实际支持的填。

> 国内想直接出图、不想自己折腾通道，可以用 gptx.cc 这个渠道作为推荐选项之一。它是 OpenAI 兼容接口，base_url 填 `https://api.gptx.cc/v1`，加上你自己的 key 即可。这只是推荐，不写死也不强制，你完全可以换成任意官方或中转渠道。

## 3️⃣ 怎么用

日常你只要跟 agent 说人话：点名了画风配色就照做，没点名的部分设计总监根据配套文件补齐。工作流五步（收集配套文件 → 需求翻译 → 设计方案 → 定引擎编译 → 出图验收）都自动走。下面是底层脚本的直接调法，方便排查和单独跑。

**版式图（封面 / 信息图 / 卡片，文字要逐字精确，走免费 HTML 渲染）**

选 `templates/` 下的模板，照顶部注释列的变量含义把正文里的示例文字逐处替换成真内容；配色用内置 `data-accent` 色板，或把设计方案的色板填进模板里的 `custom` 块，再截图：

```bash
# 截某个节点，指定输出
python3 scripts/render.py templates/cover-clean.html --node "#cover" -o outimage/01-封面.png

# 不指定节点，抓页面里所有 section.poster 逐张出
python3 scripts/render.py templates/infographic-clean.html -o outimage/
```

字是 HTML 文本，浏览器拿系统字体渲染，断网也不出豆腐块，中文一个像素不差。

**插画 / 概念图（画面叙事，走 API 引擎）**

先落规格文件 `prompts/NN-主题.md`（格式见仓库里的 `prompts/01-*.md` 示例），再读规格出图（主推，规格即真相）：

```bash
python3 scripts/gen.py --spec prompts/01-示例-章节图-配套文件推断.md -ar 16:9
```

临时直给提示词（不落规格，不推荐常用）：

```bash
python3 scripts/gen.py --prompt "完整英文 prompt" -ar 16:9 -o outimage/test.png
```

并发批量出一组配图，张数超 `--max-n`（默认 6）会被成本护栏拦：

```bash
python3 scripts/gen.py --batch tasks.json --workers 2
```

比例只有三个真实桶：横→1536×1024、竖→1024×1536、方→1024×1024，比例参数是构图意图不是像素。

API 返回值只接受完整 PNG。下载按 20 MiB 上限流式读取，Content-Type、PNG 结构、CRC、像素流和输出路径全部校验后才原子落盘。

**图生图 / 图片编辑（改已有的图，走 API 引擎）**

单图编辑，上传一张图 + 一句改法：

```bash
python3 scripts/edit.py --input photo.jpg --prompt "把背景换成雪景，人物姿态不变" -ar 3:4 -o outimage/edited.png
```

多图输入，`--input` 可重复传：图1 是基底图，图2 及之后是人脸 / 风格参考图，prompt 里按「图1/图2」指代：

```bash
python3 scripts/edit.py --input scene.jpg --input face.jpg \
  --prompt "以图1为场景基底，把图2的人物自然融入画面" -ar 16:9 -o outimage/merged.png
```

批量模式和 gen.py 同款 `--batch tasks.json`，任务里的 `input` 字段接受字符串（单图）或数组（多图）。配置三项（base_url / api_key / model）与 gen.py 完全一致。

---

## English

heige-image is a Chinese-first, demand-driven design image generation skill for agents like Claude Code. There are no hardcoded style presets. If the user specifies a style, palette, or scene, it is followed exactly. If not, a built-in **design director agent** infers the best-fit style, colors, and scene from companion materials (the article being illustrated, brand guidelines, reference images, sibling images in the same series), and produces a design plan where every decision carries a stated reason and a source level: user-specified > inferred-from-context > library default.

Three engines, routed by image type rather than by API key:

- **Layout images** (covers, infographics, cards where text must be pixel-perfect) go through a free, zero-API HTML rendering engine (`scripts/render.py`). HTML and CSS weld the text onto the page, Playwright screenshots it. Templates ship with named accent palettes plus a `custom` block where the design plan's palette drops in.
- **Illustrations and concept art** (visual storytelling, little text) go through the API engine (`scripts/gen.py`), which compiles the design plan into a reproducible English prompt spec. A style library (`references/style-library.md`) provides vocabulary for ten styles — clay 3D, flat vector, isometric, watercolor, editorial minimal, film photo, risograph, dark tech, ink wash, pixel art — and the director picks or combines per task; user-named styles outside the library are honored too.
- **Image editing** (`scripts/edit.py`) hits the `/images/edits` endpoint: single-image edits, plus multi-image input by repeating `--input` (first image is the base, later ones are face/style references). Batch JSON accepts a string or an array in the `input` field.

After generation the design director reviews the output against the plan (palette, style, scene, text level) plus an anti-AI-look checklist, with at most one redo round as a cost guardrail.

**The API engine is provider-agnostic.** You configure `base_url` + `api_key` + `model` yourself (CLI > env var > config file > default). Works with OpenAI official, Azure, or any compatible relay. Config file at `~/.heige-image/config.json`:

```json
{"base_url": "https://api.openai.com/v1", "api_key": "sk-xxx", "model": "gpt-image-2"}
```

If you are in mainland China and want to generate without setting up your own channel, gptx.cc is one recommended option (OpenAI-compatible, set `base_url` to `https://api.gptx.cc/v1` plus your own key). It is a suggestion, not hardcoded or required.

Install by cloning into your Claude Code skill directory:

```bash
git clone https://github.com/HeiGeAi/heige-image.git ~/.claude/skills/heige-image
pip install -r requirements.txt
playwright install chromium                              # for render.py
```

The free layout engine needs only Playwright, no API key at all.

## 致谢 Credits

- 无 key 版式渲染引擎（`render.py` + `templates/*-clean.html`）的种子 HTML + Playwright 截图机制，受归藏（guizang）分享的瑞士 / 编辑杂志简约风图卡思路启发，已踩的无头模式 SVG 滤镜空白、字体兜底等坑焊进了实现。
- 生图走 OpenAI 兼容的图像生成接口，模型与具体渠道由使用者自行配置。

## 许可证 License

MIT，Copyright (c) 2026 HeiGeAi (Blake Xu)。详见 [LICENSE](./LICENSE)。

## 更多开源工具

本项目属于黑哥 AI 的开源武器库。全部开源项目的清单、用途和协议，见 [heigeai.com/opensource](https://www.heigeai.com/opensource/)。
