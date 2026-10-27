# genshin

原神配队与养成推荐引擎（自用）。给定一个角色，推荐合理的队伍和每个队员的养成方案；缺角色时找替补并说明理由；区分不同命座下的配队差异。

设计方案见项目文档《原神配队与养成 AI 助手：实现方案》。本仓库目前有知识库格式、推荐引擎、B 站攻略提取管线，以及本机网页问答（DeepSeek 听懂问题、解释引擎结果）。

## 用法

```bash
pip install -e ".[dev]"
python -m genshin_assistant validate                       # 校验知识库
python -m genshin_assistant build 阿罗夏 --cons 1          # 养成方案；drafts/builds/ 里的队友也能查
python -m genshin_assistant recommend 水龙                  # 0 命，假定拥有全部角色
python -m genshin_assistant recommend 那维莱特 --cons 1 --missing 钟离,万叶
python -m genshin_assistant recommend 那维莱特 --own 芙宁娜,砂糖,白术 --json   # 输出给大模型用的 JSON
pytest
```

## 网页问答（`web` 命令）

```bash
python -m genshin_assistant web          # 打开 http://127.0.0.1:8765/ ，只在本机监听
python -m genshin_assistant chat         # 同样的问答，命令行版
```

DeepSeek key 依次找：`--key-file`、环境变量 `DEEPSEEK_API_KEY`、仓库目录或上一级目录的 `ds_key.txt`。

流程是「大模型解析问题 → 引擎算 → 大模型照着引擎结果解释」：配什么人、用什么装备都由引擎和知识库决定，大模型只负责听懂问题（角色、命座、没有谁）和把结果讲成中文，提示词里要求它不能添加引擎结果之外的角色和装备。每条回答下面可以展开「引擎结果」看原始数据。大模型解释失败时直接显示引擎结果排成的文字。

- **我的角色**：页面右侧勾选拥有的角色和命座，存在 `work/roster.json`（不进仓库）。没保存过就按「全都有、0 命」算；问到的那个角色总是当作拥有。
- **追问**：「那没有万叶呢」会沿用上一轮的角色、命座和缺的角色。
- **知识库里还没有队伍模板的角色**（目前五个新角色都是）：给出配装，再按角色标签讲适合什么样的队友，不会凭空点名。
- 代码在 `src/genshin_assistant/chat/`：`assistant.py` 是和界面无关的问答逻辑，`web.py` + `static/index.html` 是网页，以后换成聊天机器人只要另写一层界面。

## 知识库（`data/`）

| 路径 | 内容 |
| --- | --- |
| `tags.yaml` | 功能标签词表，其他文件只能引用这里的标签 |
| `characters/` | 角色：元素、武器、玩法，每个玩法的 `provides`（提供的功能）和 `needs`（对队友的需求），都可以带 `when` 条件（如 `cons < 1`） |
| `teams/` | 队伍模板：槽位、槽位功能分级（`core` / `major` / `minor`）、候选人、整队限制、攻略给出的替补结论 |
| `builds/` | 养成方案：按角色 × 玩法 × 命座区间（如阿罗夏 `0` 和 `1-6` 两档），武器档次、圣遗物组合（`set: mixed` 表示散搭）、主副词条、充能区间和随队伍变化的说明（`energy_recharge_notes`）、面板目标、天赋（`talents: {level: [...], skip: [...]}`，只分要点和不用点，不排优先级）、出处（`evidence`：UP 主、BV 号、时间点、原话）。五个新角色的配装是从 B 站攻略草稿整理的，待审 |
| `drafts/builds/` | 还在 `drafts/characters/` 的队友的配装草稿，引擎不读；逐项标了 `[攻略]`（视频里讲的）或 `[常识]`（AI 按通用配装补的），审核后和角色标签一起挪进 `builds/` |
| `equipment_buffs.yaml` | 会给队友加成的圣遗物套装和武器（翠绿之影、讨龙英杰谭、终末嗟叹之诗等）。角色配装首选了这些装备时，加成算到这个角色头上 |
| `drafts/characters/` | `draft-tags` **自动起草**的角色标签（关键词匹配天赋原文，分数一律 0.4），不参与配队；审核后挪到 `characters/` |
| `mvp_characters.yaml` | 第一版要覆盖的主C名单和录入进度 |
| `game/` | **自动生成**的游戏本体数据：每个角色一个文件（天赋原文与 10/13 级倍率、固有天赋、命座、90 级属性、官方一句话定位、技能里的名词解释、能开启的月曜/星烁反应、魔女的课业），`weapons.yaml`（90 级数值、R1/R5 效果），`artifacts.yaml`（2/4 件套效果），`glossary.yaml`（游戏内名词解释） |
| `mechanics/` | 机制知识。`official_*.yaml` 是自动导入的游戏内教程和元素共鸣原文；`damage.yaml`（伤害公式）、`reactions.yaml`（元素反应，含月曜、星烁）、`team_systems.yaml`（月兆、魔导·秘仪、辉映·星烁、元素附着、ICD 等）是手写的，不确定的地方标了 `verified: false` |

`validate` 会核对 `characters/`、`builds/`、`drafts/builds/` 里的角色、武器、圣遗物中文名是否和游戏数据一致，武器类型是否和角色对得上。

所有手写数据都带 `verified` 字段，`false` 表示草稿，还没按攻略核对。现有的那维莱特队数据就是草稿。

## 游戏数据

来源是 [genshin-db](https://github.com/theBowja/genshin-db)（npm 包，整理好的多语言数据）和 [AnimeGameData2](https://gitlab.com/Dimbreath/animegamedata2)（游戏解包数据，提供名词解释、教程、元素共鸣、文本表），两者都已更新到 7.1。下载内容缓存在 `.cache/gamedata/`，不进仓库。

```bash
python -m genshin_assistant import-gamedata              # 新版本上线、上游更新后重跑，生成 data/game/
python -m genshin_assistant game 桑多涅                   # 也可以查武器、圣遗物套装、名词（如 擢升）
python -m genshin_assistant game 水龙 --section cons      # 知识库里的别名也能用
python -m genshin_assistant enablers 星超导               # 哪些角色能为队伍开启这个反应
python -m genshin_assistant hints 沃雅妮莎                # 从天赋原文里找定位线索，录入 characters/ 时参考
python -m genshin_assistant draft-tags                   # 给知识库里还没有的角色起草标签，写到 drafts/characters/
```

已知缺口：genshin-db 不含「魔女的课业」解锁的额外天赋和技能变化，角色数据里只有从文本表找到的「魔导·秘仪」说明（`witch_quest` 字段）。官方一句话定位（`intro`）只有较新的角色有。

## 规则

- **命座**：`when: "cons >= 1"` 这类条件决定某个功能、需求或玩法是否生效。
- **核心功能**：候选人缺少槽位的 `core` 功能就不能入槽；槽位没人能填时整套模板判为不成立。
- **整队限制**：写在模板的 `constraints` 里，可用函数有 `count(元素)`、`count_any([元素...])`、`max_count([元素...])`、`has_tag(标签)`、`has_char(角色)`，例如 `count('anemo') == 2 and max_count(['pyro', 'cryo', 'electro', 'hydro']) == 2`。
- **角色自带的配队偏好**（写在角色玩法下，模板不用重复写）：
  - `needs`：需要队友提供的功能，`count` 表示需要几名队友（不算自己）。每个角色自动带 `element:元素` 标签，所以「需要水元素队友」写成 `element:hydro`。
  - `avoids`：排斥的队友功能，例如莱欧斯利排斥 `healer`，队里有人提供时按 `weight` 扣分。
  - `team_rules`：整队规则，写法同 `constraints`。`hard: true` 不满足时队伍不成立，否则按 `weight` 扣分，例如法尔伽的「双风双X」、丝柯克的水冰队友。
  - `field: on_field / off_field / flex`：站场、后台、都可以。一队里有多名 `on_field` 角色时扣分。
  - 以上都可以带 `when`（命座条件）、`reason`（给人看的理由）、`evidence`（天赋原文）、`source`（攻略出处）。
- **带参数的标签**：`buff:crit_dmg`（按伤害乘区分的增益：固定攻击、百分比攻击、百分比生命、倍率附加、增伤、暴击率、暴击伤害、擢升、精通）、`shred:hydro`（降水抗）、`application:cryo`（后台挂冰）、`element_dmg_buff:cryo`、`reaction_buff:stellar_swirl`、`enables:lunar_charged`。参数会对照元素、乘区列表和 `mechanics/reactions.yaml` 校验。槽位功能可以写 `buff:*`，表示任意一种增益。
- **分数**：1.0 是标杆，0.7 是常见替代，0.4 是顺带有；`value` 写大致数值（百分比写小数）。
- **自身能力**：`traits` 写只给自己的能力（例如薇斯纳巡风列装自带抗打断），能满足自己的同名 `needs`。
- **自动标签**：`element:元素`、`enables:反应`、`witch_arcana`（魔导）、`moonsign`（月兆）、`nightsoul`（夜魂）由游戏数据决定，不用手写。
- **替补**：候选名单之外、满足全部核心功能的角色也会被自动考虑（基础分 0.5）；`substitutions` 里写的攻略结论会作为替补理由优先展示。

## B 站攻略提取（`bili` 命令）

从 B 站攻略视频的简介、置顶评论和 AI 字幕里，用 DeepSeek 抽出结构化草稿，**人审之后再手工合入 `data/`**，程序永远不直接改知识库。
B 站接口部分移植自 [dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale)（MIT），见 `THIRD_PARTY_NOTICES.md`。

### 准备

1. `pip install -e ".[bili]"`（装 pypinyin，用来纠正字幕里的同音错字）。
2. B 站 cookie：**建议用小号**。浏览器登录后按 F12 → Application → Cookies 复制 `SESSDATA`、`bili_jct`、`buvid3`，
   在仓库根目录新建 `bili-credentials.json`（已在 `.gitignore` 里，不会被提交）：`{"sessdata": "...", "bili_jct": "...", "buvid3": "..."}`
   （也认 `~/.genshin-assistant/bili-credentials.json`、dsh-bili-whale 的 `~/.bili-ai/credentials.json`，
   或环境变量 `BILI_SESSDATA` / `BILI_JCT` / `BILI_BUVID3`）。没有登录拉不到 AI 字幕。
3. `DEEPSEEK_API_KEY` 环境变量。可选 `DEEPSEEK_MODEL`（默认 `deepseek-chat`）。
4. 抽帧才需要 ffmpeg。
5. 可选：有些 UP 主的视频没有任何字幕。把 `sources.yaml` 的 `asr` 设成 `whitelist`（或 `all`）并 `pip install -e ".[asr]"`，
   就会下载音轨，用本机 faster-whisper 转写（第一次会下载语音模型）。
6. 可选：字幕剪进画面里的（硬字幕），把 `ocr` 设成 `whitelist` 并 `pip install -e ".[ocr]"`，
   会下载 480p 视频、每秒截一帧字幕区域做 OCR（画面没变的帧跳过，每帧都有的进度条等固定文字会去掉），读完删除视频。
   硬字幕优先于语音识别。字幕位置用 `ocr_crop` 调，个别 UP 主不一样就写进 `ocr_crop_up`。
   CPU 每张截图约 1.5 秒；有 N 卡就 `pip uninstall -y onnxruntime` 再 `pip install "onnxruntime-gpu[cuda,cudnn]"`
   （只要装好显卡驱动，CUDA 库随 pip 一起装），会自动改用显卡。其他显卡在 Windows 上可以装 `onnxruntime-directml`。

### 用法

```bash
python -m genshin_assistant bili status                 # 登录态、今日读预算、是否在风控熔断
python -m genshin_assistant bili run                    # 跑 sources.yaml 里的试点角色：取材 + 抽取
python -m genshin_assistant bili harvest 那维莱特        # 只取材
python -m genshin_assistant bili extract 那维莱特        # 只抽取（读本地取材结果）
python -m genshin_assistant bili frames BV1xxxx         # 在字幕提到配装的时间点抽帧拼图
```

产物都在 `work/`（已在 `.gitignore`，抓到的内容不要提交或公开分发）：

| 路径 | 内容 |
| --- | --- |
| `work/bili/<角色>/<BV>.json` | 取材结果：简介、置顶评论、带时间戳的字幕 |
| `work/bili/<角色>/<BV>.claims.json` | 单个视频的抽取结果（缓存，`--refresh` 重抽） |
| `work/drafts/<角色>-<时间>.yaml` | 待审草稿：逐条把 `review` 改成 accept / edit / reject |
| `work/drafts/<角色>-<时间>.md` | 同一份草稿的阅读版，引用可以点开跳到视频对应秒数 |

### 流程

1. **选视频**：按 `data/bili/sources.yaml` 的搜索词搜，只留标题含角色名、上线日期之后、两分钟以上的视频，白名单 UP 主优先，其余按播放量，每个角色默认 8 个。
2. **取材**：简介 + 置顶评论（和 UP 主自己的评论）+ AI 字幕。
3. **名词归一**：用知识库和 `data/bili/glossary.yaml` 的名词表纠正字幕错字（三个字以上的名字按拼音自动纠，两个字的要显式开启）。
4. **抽取**：DeepSeek 按固定 JSON 结构输出七类结论（推荐队友、槽位功能分级、整队限制、替补、命座影响、玩法、养成）。功能标签只能用 `tags.yaml` 里的，词表外的会标出来。每条结论必须带逐字引用，程序在材料里找到引用才保留，并按字幕行补上时间戳；找不到就丢弃。
5. **跨视频汇总**：同一结论按视频数计票；意见不一（例如一个说核心、一个说加分）标为“有争议”；不同命座、玩法分开计票。
6. **对照知识库**：每条结论标“一致 / 冲突 / 知识库没有”，并列出知识库里有、视频都没提到的内容。试点就用它衡量 AI 草稿的准确率。

**必须人审**：功能分级（core / major / minor）和整队限制视频里很少直说，每条都会被标出来；另外有争议、只有一个视频提到、模型推断、出现未知角色或词表外功能的结论也会标出来。

### 请求保护

两次请求至少隔 1.5 秒，每天最多 3000 次读请求，撞到 412 / 风控码立刻熔断 5 分钟（连续撞翻倍，封顶 30 分钟）。
视频信息缓存 24 小时，取过材的视频不会再请求。一个视频大约 4 到 5 次请求。只做读操作，不评论、不点赞、不发私信。
