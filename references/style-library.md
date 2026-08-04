# 风格库（设计总监的选型货架）

设计总监选画风时来这查。**这是货架，只提供词汇和判断依据。** 每次出图用哪个风格，由「用户指定 > 配套文件推断 > 库默认」三级链决定，任何词条都无权自动上岗。

用户点名库外风格（比如吉卜力、蒸汽波、孟菲斯）也照做：按词条同样的五个字段现场拆解，拆完记进设计方案，用顺了可以补进本库。

---

## 怎么用

1. 按「适用场景」列筛出候选，结合 brief 的情绪维度定一个。
2. 把词条的「prompt 词汇」整段抄进最终 prompt 的风格段，hex 按设计方案色板替换。
3. 「禁忌项」并进 prompt 的负向收尾。
4. **组合规则**：最多组合两个词条，且必须一主一辅（比如等距 2.5D 为主、软萌材质为辅）。三个起步必糊，等于没有风格。

---

## 词条

### 1. 软萌 3D（粘土 / 软胶质感）

- **一句话定位**：手工捏制的哑光立体质感，圆润治愈，看着想摸一下。
- **适用场景**：新手教程配图、治愈系内容、吉祥物示意、要降低门槛感的技术话题。
- **prompt 词汇**：`Clay 3D render, matte handmade plasticine texture, soft sculpted rounded surfaces, chubby inflated volume, soft diffused studio lighting, subtle subsurface scattering, clean background.`
- **配色倾向**：低饱和暖色系最稳（粉 / 奶油 / 米白一类），底色干净不抢戏。
- **禁忌项**：`no glossy plastic, no metal, no harsh shadows, no high saturation.` 一滑成反光塑料 C4D 感就废。

### 2. 扁平插画（flat / vector）

- **一句话定位**：色块加简洁线条，信息感强，现代干净。
- **适用场景**：产品功能示意、流程概念、企业官网感的配图、要快速看懂的抽象概念。
- **prompt 词汇**：`Flat vector illustration, clean geometric shapes, bold color blocks, minimal line details, smooth edges, generous negative space.`
- **配色倾向**：2 到 3 个饱和度中等的主色加大面积浅底，色块之间对比清晰。
- **禁忌项**：`no gradients overload, no 3D shading, no clutter.` 元素一多就变素材站贴纸拼盘。

### 3. 等距 2.5D（isometric）

- **一句话定位**：俯视 45 度的小世界感，适合展示系统和结构。
- **适用场景**：架构图意象、工作流全景、多模块产品鸟瞰、「一张图看懂整个系统」。
- **prompt 词汇**：`Isometric 2.5D illustration, 45-degree top-down view, miniature world feel, clean geometric structures, consistent perspective grid, soft ambient occlusion.`
- **配色倾向**：一个主色统摄全场，模块用同色系深浅区分，底色浅灰或浅蓝白。
- **禁忌项**：`no mixed perspectives, no fisheye distortion.` 透视一乱整张垮掉。

### 4. 水彩手绘（watercolor）

- **一句话定位**：颜料晕染加留白，温润有呼吸感，人味最重。
- **适用场景**：情感向内容、生活方式、人物故事、需要「不像 AI 画的」的场合。
- **prompt 词汇**：`Watercolor illustration, soft pigment bleeding, visible paper texture, loose expressive brushstrokes, generous white space, hand-painted imperfections.`
- **配色倾向**：低饱和透明感，两三个主色互相晕染，大量留白当底。
- **禁忌项**：`no digital airbrush look, no hard outlines, no neon colors.` 边缘一硬就露馅。

### 5. 编辑杂志简约（editorial minimal）

- **一句话定位**：瑞士版式那套，网格、留白、克制强调色，字是主角。
- **适用场景**：封面、信息图、金句卡、数据卡。**这是版式引擎 render.py 的默认气质**，模板已内置，逐字精确的文字活优先走它。
- **prompt 词汇**（API 侧要这个味时用）：`Swiss editorial design, minimal grid layout, generous white space, single restrained accent color, hairline dividers, strong typographic hierarchy.`
- **配色倾向**：中性底（米白 / 浅灰）加一个强调色，整张不超 3 色。
- **禁忌项**：装饰形状堆砌、强调色超过一个、留白被填满。简约的命门是克制。

### 6. 胶片写实（photo-real film）

- **一句话定位**：真实照片质感加胶片颗粒，可信度最高。
- **适用场景**：新闻时效类配图、真实场景还原、人物 / 产品实拍感、反 AI 味诉求最强的场合。
- **prompt 词汇**：`Photorealistic, 35mm film photography, natural lighting, shallow depth of field, subtle film grain, candid documentary framing.`
- **配色倾向**：跟随真实光线，后期只压一层轻微胶片色调，禁止滤镜感过重。
- **禁忌项**：`no studio-perfect lighting, no plastic skin, no oversaturated HDR.` 完美打光反而假。

### 7. 复古印刷（risograph / 丝网）

- **一句话定位**：孔版印刷的错位套色和颗粒，复古有态度。
- **适用场景**：活动海报、态度型内容、独立品牌感、想跳出「干净数字感」的场合。
- **prompt 词汇**：`Risograph print style, limited 2-3 color separation, visible grain and ink texture, slight misregistration, retro poster composition.`
- **配色倾向**：2 到 3 个高识别度专色（荧光粉 / 蓝 / 橙常见），纸白底。
- **禁忌项**：`no smooth gradients, no photorealistic details.` 颜色一多就丢掉印刷感。

### 8. 暗色科技（dark tech）

- **一句话定位**：深底加克制的亮色光效，专业冷静的技术气场。
- **适用场景**：开发者工具、基础设施话题、发布会感的产品图、深色界面产品的配图。
- **prompt 词汇**：`Dark background tech aesthetic, deep charcoal base, restrained glowing accents, clean geometric lines, subtle depth fog, premium minimal composition.`
- **配色倾向**：深灰黑底加一个亮色强调（电青 / 亮绿 / 琥珀选一），亮色占比压在 10% 以内。
- **禁忌项**：`no cyberpunk neon overload, no purple-blue gradient cliche.` 蓝紫渐变发光是 AI 生图最烂大街的投降色，离远点。

### 9. 水墨国风（ink wash）

- **一句话定位**：墨色浓淡加大片留白，东方意境。
- **适用场景**：传统文化话题、哲理向内容、中式品牌、需要意境而非信息密度的场合。
- **prompt 词汇**：`Chinese ink wash painting, expressive brush strokes, ink gradient from deep black to pale gray, vast negative space, minimal color accents, rice paper texture.`
- **配色倾向**：墨色黑灰为主，至多一个传统色点缀（朱砂 / 黛青），纸白留白过半。
- **禁忌项**：`no dense composition, no western oil painting texture.` 画满就丢掉意境。

### 10. 像素风（pixel art）

- **一句话定位**：8-bit 游戏像素颗粒，怀旧极客味。
- **适用场景**：游戏话题、程序员向内容、怀旧梗、轻松诙谐的技术自嘲。
- **prompt 词汇**：`Pixel art, 8-bit retro game style, limited color palette, crisp pixel edges, simple iconic shapes, side-scroller composition.`
- **配色倾向**：8 到 16 色的受限色板，对比明快。
- **禁忌项**：`no anti-aliasing blur, no mixed resolutions.` 像素颗粒大小必须统一。

---

## 快查表（载体 + 情绪 → 候选风格）

没有任何配套文件和用户指定时，从这张表选库默认：

| 情绪 \ 载体 | 封面 / 信息图（版式引擎） | 章节图 / 概念图（API 引擎） | 海报 / 态度图 |
|---|---|---|---|
| 专业冷静 | 编辑杂志简约 | 等距 2.5D / 扁平插画 | 暗色科技 |
| 治愈亲和 | 编辑杂志简约（暖色板） | 软萌 3D / 水彩手绘 | 水彩手绘 |
| 真实可信 | 编辑杂志简约 | 胶片写实 | 胶片写实 |
| 态度锐利 | 编辑杂志简约（高对比色板） | 复古印刷 | 复古印刷 |
| 轻松诙谐 | 编辑杂志简约 | 扁平插画 / 像素风 | 像素风 |
| 东方意境 | 编辑杂志简约（留白加倍） | 水墨国风 | 水墨国风 |

版式引擎那列全是编辑杂志简约，原因写在 SKILL.md 任务路由：逐字精确的文字活只有 HTML 渲染稳，风格差异靠色板和排版参数体现，配色由设计方案定。
