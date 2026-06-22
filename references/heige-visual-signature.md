# 黑哥视觉签名（heige-visual-signature）

> heige-image 的护城河之一。这一套定死「黑哥出的图长什么样」，目标是别人一眼能认出来。
> 这是黑哥 AI 账号的专属画风，**粘土 3D 卡通**：粉白圆润小机器人那套，章节漫画和 16:9 总结图都靠它锚住识别度。治愈温暖路线，靠可爱和亲和力赢。

---

## 一句话定义

**暖粉加奶油白的粘土材质 3D 小机器人，圆润可爱、手工捏制质感、柔光治愈，站在干净米白背景里，看着就让人想摸一下。**

风格不靠信息密度赢，靠亲和力赢。每张图都像一个治愈系玩偶摆拍，门槛感清零，让看 AI 教程的人先放下戒备。看到这种粉白圆乎乎的粘土小机器人，就该想到是黑哥。

---

## 一、主色板（给死 hex，照着用）

暖粉加奶油白是这套签名最强的识别点。整张图压在低饱和暖色域里，不撞色，不刺眼，要的就是奶感和柔和。

| 角色 | 颜色 | Hex | 用法 |
|------|------|-----|------|
| 主调暖粉 | Warm Blush Pink | `#F5A8B8` | 机器人腮红、耳机、披风、爱心、标题字、点缀道具 |
| 浅樱粉 | Soft Cherry Pink | `#F7C6CE` | 粉色过渡、气泡边、图标底、阴影里的暖色 |
| 奶油白 | Cream White | `#FBF3EC` | 机器人身体主面、留白、道具底色 |
| 米白背景 | Off-White Background | `#F6ECE4` | 整张图的干净背景，带一点点暖灰 |
| 暖棕黑 | Warm Brown Black | `#3D2B2B` | 屏幕脸（深棕黑屏底）、眼睛、嘴、轮廓暗部，禁用纯黑 |
| 点缀玫红 | Accent Rose | `#E98AA0` | 最重的那一笔粉，红心、勾选标记、关键图标 |

辅助色（撑场景，不抢主色，全部低饱和奶化）：

| 角色 | 颜色 | Hex | 用法 |
|------|------|-----|------|
| 奶黄 | Milky Yellow | `#F2D9A0` | 积木、齿轮、文件图标的暖黄块 |
| 奶蓝 | Milky Blue | `#AFC8DD` | 个别道具点缀，比例要小，别破坏暖色调 |
| 暖灰 | Warm Gray | `#D8C9BE` | 投影、扳手等中性道具 |

**配色铁律**：
- 永远低饱和暖色，永远奶感柔和，禁止高饱和撞色、禁止冷峻、禁止霓虹、禁止赛博。要的就是治愈玩偶的暖。
- 整张图色温压在暖粉到米白这一段，蓝色和黄色都要奶化降饱和，比例控制在小面积点缀，绝不让冷色抢戏。
- 英文 prompt 里固定写死这句：`Warm blush pink and cream white palette, soft low-saturation pastel tones, clean off-white background.`

---

## 二、粘土材质质感（这是命门，写不到位就出不来）

1. **手工捏制的黏土表面**。所有物体看起来像橡皮泥或软陶捏出来再 3D 渲染，表面是哑光磨砂质感，带细微的指纹压痕和不完美的手作起伏，绝不光滑反光、绝不塑料感、绝不金属感。
2. **圆角无棱**。所有边缘都被捏圆，没有任何锐利直角，连方块积木都是圆鼓鼓的胖角。
3. **柔软膨胀体积**。物体像充了气一样饱满鼓胀，体积感强，看着软乎乎可以捏。
4. **统一的粘土厚度**。图标、气泡、道具都做成有厚度的立体粘土片，不是平面贴纸，每片都微微浮起带投影。
5. 英文 prompt 固定写死：`Claymation / clay 3D render, matte handmade plasticine texture, soft sculpted surfaces, rounded edges, no glossy plastic or metal.`

---

## 三、3D 渲染光感

1. **柔光无硬影**。整体打柔和的环境光加一个轻微的顶光，过渡平滑，禁止硬边阴影、禁止强烈高光点。
2. **暖调全局照明**。光本身带暖色，给奶油白面打上一层淡淡的暖粉环境色。
3. **接触阴影柔糊**。物体落地处有一团柔软发散的浅色投影，托住体积，绝不是清晰的黑色硬投影。
4. **微弱次表面散射**。粘土边缘透一点点光，更像真实软陶，增强治愈温润感。
5. 英文 prompt 固定写死：`Soft diffused studio lighting, warm ambient glow, gentle soft shadows, subtle subsurface scattering, no harsh highlights.`

---

## 四、招牌形象：粉白小机器人（黑哥吉祥物）

这只小机器人是签名的人格锚点，每张主图尽量让它出场。

1. **头部**：奶油白圆头盔，戴一副暖粉耳机，头顶一根细天线，天线顶端一颗粉色小爱心。
2. **脸**：一整块圆角的深棕黑屏幕当脸（屏底用 `#3D2B2B`，禁纯黑），上面一对发亮的圆眼睛加一张上扬的小嘴，表情治愈友好。可做眨眼、笑眼、惊喜等微表情。
3. **身体**：奶油白胖乎乎的圆身子，胸口一颗暖粉立体小爱心，配暖粉小披风或袖口点缀。
4. **比例**：大头小身、四肢短圆的 Q 版幼态比例，整体像个能抱在手里的玩偶。
5. **动作**：站姿为主，可比手势、举道具、招手，动作幅度小而温和，永远在传达友好。
6. 英文 prompt 固定写死：`A cute chubby clay robot mascot, cream-white rounded body, blush-pink headphones and small heart on chest, dark brown screen face with glowing round eyes and a friendly smile, antenna with a tiny pink heart, big-head small-body chibi proportions.`

配套道具同款捏：粘土质感的聊天气泡、文件卡、日历、报表夹、积木、齿轮、勾选标记，全部圆角奶色，浮在主角周围当氛围元素。

---

## 五、背景与构图

1. **干净米白背景**（`#F6ECE4`），纯色或极淡的暖色渐变，不放任何复杂场景、不放纹理噪点，让主角和道具干净浮出。
2. **居中聚焦**。主角放中间或黄金分割点，道具像气泡一样环绕悬浮在四周，留足空白。
3. **悬浮飘散布局**。配件不堆叠不拼贴，各自带柔影轻轻飘在空中，疏密有致，整体透气。
4. 16:9 总结图：主角偏左或居中，右侧或上方留标题区，标题用暖粉立体粘土字，同样捏出厚度和圆角。

---

## 六、情绪与载体

**情绪**：治愈、温暖、亲和、轻松、可爱、零门槛、被照顾感。让看 AI 硬核内容的人先松一口气，觉得这事不吓人。

**适合的载体**：
- 教程系列章节配图（漫画分镜那种逐章演示）
- 16:9 文章总结图 / 头图
- 产品功能介绍的吉祥物示意图
- 公众号封面、知识科普卡片
- 任何想走「治愈降门槛」而非「锋利吐槽」的内容

**不适合**：尖锐吐槽、行业黑话讽刺、严肃数据报告、冷峻科技感主题（那些走另一套或换载体）。

---

## 七、出图 prompt 必带关键词清单

英文 prompt 里这几组词固定写死，缺一就容易跑偏：

1. 风格锚：`claymation, clay 3D render, cute kawaii style, handmade plasticine`
2. 材质：`matte clay texture, soft sculpted surfaces, rounded edges, chubby inflated volume, no glossy plastic, no metal`
3. 配色：`warm blush pink and cream white palette, soft low-saturation pastel tones`
4. 光感：`soft diffused lighting, warm ambient glow, gentle soft shadows, subtle subsurface scattering`
5. 背景：`clean off-white background, minimal, floating elements with soft shadows`
6. 主角（出现时）：`cute chubby clay robot mascot, cream-white body, blush-pink headphones, heart on chest, dark brown screen face, glowing eyes, friendly smile, chibi proportions`
7. 情绪：`heartwarming, healing, cozy, friendly, adorable`

**负向 prompt 固定写死**：`no high saturation, no neon, no cyberpunk, no harsh shadows, no glossy reflection, no metallic, no realistic photo, no dark background, no clutter.`

---

## 八、prompt 骨架（编译第三步直接套）

把 brief 编译成送进 gpt-image 类模型的英文 prompt，按这个顺序拼，每段都别省：

1. **风格前缀（写死，每张都带）**：
   `Claymation, clay 3D render, cute kawaii handmade plasticine style, matte clay texture, soft sculpted rounded surfaces, chubby inflated volume.`
2. **主体**：画面主角是什么。优先让粉白小机器人出场，再补它在做什么动作。
3. **场景与构图**：居中或黄金分割点放主角，道具悬浮环绕，留足空白，干净米白背景。
4. **氛围道具**：列出该出场的粘土小道具（积木、齿轮、文件卡、气泡、爱心、勾选标记），全部圆角奶色、带柔影。
5. **配色（写死）**：`Warm blush pink and cream white palette, soft low-saturation pastel tones, clean off-white background.`
6. **光感材质（写死）**：`Soft diffused studio lighting, warm ambient glow, gentle soft shadows, subtle subsurface scattering, no glossy plastic, no metal, no harsh highlights.`
7. **比例意图**：写构图意图（如 `cinematic ultra-wide banner composition`），真实出图桶在脚本层定。
8. **控字句**：插画隐喻图禁可读文字（`absolutely no readable text, no words, no Chinese characters`）；16:9 总结图要标题就只留一行暖粉立体粘土中文字，其余靠画面。
9. **负向收尾（写死）**：`no high saturation, no neon, no cyberpunk, no harsh shadows, no glossy reflection, no metallic, no realistic photo, no dark background, no clutter.`

控字两套，跟载体走：
- **纯视觉隐喻图**：0 可读文字，靠粉白小机器人的动作和道具讲故事。
- **16:9 总结图 / 头图**：可留一行短中文标题，做成暖粉立体粘土字，捏出厚度和圆角，≤14 字，其余别堆。

---

## 八点五、一眼识别清单（出图后逐条对）

盖掉一切上下文，单看这张图，下面三条同时成立才算黑哥的粘土签名：

1. **配色对不对**：是不是暖粉加奶油白压全场，米白背景，没有任何高饱和撞色、冷蓝紫、霓虹。
2. **材质对不对**：是不是哑光手工粘土质感，圆角无棱，软乎乎像能捏，没有塑料反光、没有金属、没有写实照片感。
3. **吉祥物在不在**（主图）：是不是那只奶油白圆身子、暖粉耳机、胸口爱心、深棕黑屏幕脸的粉白小机器人，治愈友好。

三条全中就是黑哥。少一条就回炉，回炉最多 1 轮，1 轮还不行就老实降级（换主体重写 prompt，或承认这张不该这么做）。

---

## 九、出图后验收清单（贴出去之前过）

肉眼过一遍，任意一条中招就回炉：

1. **手、字、对称物有没有崩**。手指数量、文字拼写（中文尤其，扩散模型基本写不对）、左右本该对称的物件（耳机、眼睛、四肢）这三处是崩坏重灾区，崩了直接废。
2. **粘土质感到没到位**。表面是不是哑光软陶感，别滑成塑料 3D 渲染那种反光油光。一有油光就是签名没立住。
3. **配色有没有跑出暖色域**。冒出高饱和色块、冷蓝紫、霓虹，立刻回炉，整张要压回暖粉到米白这一段。
4. **治愈劲到没到位**。看图的人会不会想摸一下、会不会松一口气觉得这事不吓人。可爱亲和是这套画风的及格线，冷峻吓人就废了。
5. **签名认不认得出**。盖掉标题单看图，认得出是黑哥这套粉白粘土吗，还是放进任何 AI 生图账号都不违和。认不出就等于白做。

### 一句话自检

出图前问：**这 prompt 是不是把暖粉奶白、手工粘土、粉白小机器人三样都写死了？**
出图后问：**这张图盖掉标题还认得出是黑哥的粘土治愈风吗？**
两个都答得硬气，才算过检。

---

核心识别点三句话锁死：**暖粉加奶油白配色、手工粘土哑光材质、粉白圆润小机器人吉祥物**，三者同时在场就是黑哥的粘土签名。
