# 生图提示词模板

每张图单独生成。根据正文内容替换变量，不要把多张图拼在一起。

```text
Generate one standalone 16:9 horizontal Chinese article illustration.

Visual DNA:
Pure white background. Hand-drawn product sketch feeling. Slightly wobbly pen lines. Lots of empty white space. Clean absurd explanatory illustration, not a poster. Use black line art for structures and objects, but keep the recurring character colorful and recognizable. Sparse handwritten Chinese annotations in orange, red, blue, green, or yellow. No gradients, no shadows, no paper texture, no complex background, no commercial vector style, no PPT infographic look, no cute mascot poster, no children's illustration, no realistic UI.

Recurring IP character required:
滚滚, a small red panda / 小熊猫 product-manager character. Orange-red fur, white facial markings, white eyebrow spots, dark brown cheeks and limbs, fluffy ringed tail, black over-ear headphones or dark hoodie when appropriate. 滚滚 is an AI-era internet product manager who writes PRDs, tunes prompts, reads dashboards, breaks down tasks, studies user feedback, and asks for evidence. 滚滚 must perform the core conceptual action, not decorate the scene. Make 滚滚 thoughtful, focused, warm, slightly deadpan, and professional, not a random raccoon, fox, robot, plush toy, or childish mascot.

Theme:
{正文配图主题}

Structure type:
{结构类型：Workflow / 系统局部 / 前后对比 / 角色状态 / 概念隐喻 / 方法分层 / 地图路线 / 小漫画分镜}

Core idea:
{这张图要表达的核心意思}

Composition:
{具体画面：滚滚在哪里、正在做什么、主要物件是什么、信息如何流动}

Suggested elements:
{元素1} / {元素2} / {元素3} / {元素4}

Chinese handwritten labels:
{标注词1} / {标注词2} / {标注词3} / {标注词4} / {可选标注词5}

Color use:
Black for structural line art, object outlines, and main text. Orange for main flow/path/arrows. Red only for key warnings/problems/results. Blue only for secondary notes, AI/system state, or assistant feedback. Green for validated paths or positive outcomes. Yellow for sticky notes, insight sparks, or highlighted user feedback. Keep colors lively but sparse. The red panda character itself should stay orange-red, white, dark brown, and black.

Constraints:
One image explains only one core structure. Keep the main subject around 40%-60% of the canvas. Preserve at least 35% blank white space. Use at most 5-8 short handwritten Chinese labels. Do not write a title in the top-left corner. Do not write the structure type on the image. Do not make it a formal diagram, course slide, dense explainer, corporate poster, or tech UI mockup. Do not copy prior examples or reuse known case compositions unless explicitly requested; invent a fresh visual metaphor for this specific article. It should be clear but not instructional, interesting but not childish, strange but clean.
```

## 图像编辑提示

去掉左上角标题：

```text
Edit the provided image. Remove only the handwritten title "{要删除的文字}" and its underline from the top-left corner. Fill that area with the same clean white background, matching the surrounding blank paper. Preserve everything else exactly: characters, labels, paths, line style, composition, aspect ratio, and image quality. Do not add any new text or objects.
```

增强滚滚参与感：

```text
Regenerate this illustration with the same core meaning and simple layout, but make 滚滚 more central to the conceptual action. 滚滚 should be doing the strange product-manager work that explains the idea, not standing beside the diagram. Keep it clean, sparse, hand-drawn, colorful but restrained, and not childish.
```
