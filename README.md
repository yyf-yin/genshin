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

Windows 上可以直接双击仓库根目录的 `start-web.bat`：第一次会建 `.venv` 并安装依赖，然后启动网页并打开浏览器，关掉黑窗口就停。

DeepSeek key 依次找：`--key-file`、环境变量 `DEEPSEEK_API_KEY`、仓库目录或上一级目录的 `ds_key.txt`。

流程是「大模型解析问题 → 引擎算 → 大模型照着引擎结果解释」：配什么人、用什么装备都由引擎和知识库决定，大模型只负责听懂问题（角色、命座、没有谁）和把结果讲成中文，提示词里要求它不能添加引擎结果之外的角色和装备。每条回答下面可以展开「详细数据」看引擎的原始结果。大模型解释失败时直接显示引擎结果排成的文字。

最后一步是**派蒙改写**：DeepSeek 的回答交给本机的派蒙微调模型（`D:\projects\model` 的 `scripts/client-server.py`，Qwen3-1.7B + QLoRA，默认 `http://127.0.0.1:8000`）逐段改写，网页对话框的名牌是「派蒙」。改写服务要先单独开着（例如在模型项目里运行 `start-client.ps1`）；没开、没加载好或超时（一个回答最多等 120 秒）就直接显示原回答。小模型会截断人名、改数字、加原文没有的话，所以每段改写都要过检查：角色、武器、圣遗物名一个不少不多、没被截短，命座、件数和数字不变，篇幅没有暴涨，不过关的段落保留原文。意思被改反（如「可以稍后升」变「先升」）这类问题检查不出来。`--no-paimon` 关掉改写，`--paimon-url` 或环境变量 `PAIMON_URL` 换服务地址。

- **角色按全都有算**：网页不设「我的角色」，推荐时当作所有角色都有，四星队友的 0 命和 6 命并排给。没有谁直接在问题里说（「我没有芙宁娜」）。命令行 `chat` 仍然读 `work/roster.json`（`--roster` 可改）。
- **追问**：「那没有万叶呢」会沿用上一轮的角色、命座和缺的角色。
- **知识库里还没有队伍模板的角色**（目前五个新角色都是）：给出配装，再按角色标签讲适合什么样的队友，不会凭空点名。
- 代码在 `src/genshin_assistant/chat/`：`assistant.py` 是和界面无关的问答逻辑，`web.py` + `static/index.html` 是网页，以后换成聊天机器人只要另写一层界面。

## 伤害计算（`damage` 命令）

```bash
python -m genshin_assistant multipliers 那维莱特 --level 10           # 列出天赋倍率，写场景时查名字
python -m genshin_assistant damage examples/damage/neuvillette.yaml   # 按场景算，逐个乘区列出来
python -m genshin_assistant damage examples/damage/neuvillette.yaml --json
```

公式：基础伤害（倍率 × 攻击/生命/防御/精通 + 固定值）× 增伤区 × 暴击区 × 防御区 × 抗性区 × 增幅反应区 × 擢升区。剧变、激化、月曜反应按各自公式算，说明和出处在 `data/mechanics/damage.yaml`、`reactions.yaml`，代码在 `src/genshin_assistant/damage/`。

场景文件（例子见 `examples/damage/`）写角色、等级、天赋等级、武器、圣遗物（套装、三个主词条、副词条合计或词条数）、增益、敌人和要算的几下伤害：

- **自动算的**：角色 90 级白值和突破属性、武器白值和副属性、圣遗物主词条、无条件的 2 件套、天赋 1~15 级倍率、等级系数。
- **要自己写进 `buffs` 的**：武器特效、4 件套、角色固有天赋和命座、队友增益、减抗减防。没写的武器特效和 4 件套会在输出最后提示原文。属性名用中文（`水伤`、`重击增伤`、`暴击率`、`减抗`、`蒸发加成`、`擢升`……），百分比直接写数字，20 表示 20%。
- **hits**：`talent` 写天赋行名（可以只写一部分，重名加 `爆发:` 这类前缀），可选 `reaction`（蒸发/融化/超激化/蔓激化）、`base_multiplier`（如遗龙之荣 1.6）、`times`、`variant`（低空/高空）、`element`（附魔）、`buffs`（只对这一下生效）。名字带「月感电伤害」等的天赋行自动按月曜直伤算，其他月曜直伤写 `lunar: 月感电`。单独算一次剧变或月曜反应写 `reaction: 超载`（扩散要加 `element`）。
- **星烁反应**：星超导系数按叠满后的 2.0（反应本身 × 等级系数，角色技能里的星超导伤害 × 倍率）；星扩散直伤系数 1.0，星扩散反应本身的系数未知。和月曜一样不吃防御和普通增伤（社区资料，待实测）。
- **还不能算的**：星扩散反应本身；角色 90 级以外的白值；天赋、命座、武器特效的自动解析。命座加的 3 级天赋要自己加在 `talents` 里。

### 配队排序用伤害

推荐引擎给每支候选队伍算「全队一轮（20 秒）的期望总伤害」：每个角色的循环和给队友的增益写在 `data/damage/profiles/`（写法见 `data/damage/README.md`），面板用 builds 的首选配装（让出增益装备后的那套）和统一副词条。增益带条件（例如菲谢尔的魔导·秘仪只在有超载、感电时生效），风套这类只对一种元素生效的增益由引擎逐个元素试、取总伤害最高的。

有伤害数据时，队伍得分 = 总伤害 / 本次最高总伤害 − 没满足的需求和冲突的扣分；主C没有循环数据时仍按标签打分。队员没有循环数据时伤害按 0 算，输出里会列出来。目前有数据的：莱欧斯利、奥黛塔、菲谢尔、八重神子、砂糖、希诺宁（AI 草稿，次数按天赋原文估）。

## 知识库（`data/`）

| 路径 | 内容 |
| --- | --- |
| `tags.yaml` | 功能标签词表，其他文件只能引用这里的标签 |
| `characters/` | 角色：元素、武器、玩法，每个玩法的 `provides`（提供的功能）和 `needs`（对队友的需求），都可以带 `when` 条件（如 `cons < 1`） |
| `teams/` | 队伍模板：槽位、槽位功能分级（`core` / `major` / `minor`）、候选人、整队限制、攻略给出的替补结论 |
| `builds/` | 养成方案：按角色 × 玩法 × 命座区间（如阿罗夏 `0` 和 `1-6` 两档），武器档次、圣遗物组合（`set: mixed` 表示散搭）、主副词条、充能区间和随队伍变化的说明（`energy_recharge_notes`）、面板目标、天赋（`talents: {level: [...], skip: [...]}`，只分要点和不用点，不排优先级）、出处（`evidence`：UP 主、BV 号、时间点、原话）。五个新角色的配装是从 B 站攻略草稿整理的，待审 |
| `drafts/builds/` | 还在 `drafts/characters/` 的队友的配装草稿（一人一个文件，2026-10-27 从五个新角色队里 28 个队友的 B 站攻略整理），引擎不读；逐项标了 `[攻略]`（视频里讲的）或 `[常识]`（AI 按通用配装补的），审核后和角色标签一起挪进 `builds/` |
| `drafts/teams/` | 队伍模板草稿（五个新角色攻略里的队伍，按主 C 整理），引擎不读：候选人里有还在 `drafts/characters/` 的队友，标签审核后连同模板挪进 `characters/`、`teams/` |
| `damage/profiles/` | 伤害循环：一轮放哪些技能、打几下、触发什么反应，给自己和队友的增益（配队排序用，AI 草稿） |
| `equipment_buffs.yaml` | 会给队友加成的圣遗物套装和武器（翠绿之影、讨龙英杰谭、终末嗟叹之诗等）。角色配装首选了这些装备时，加成算到这个角色头上 |
| `drafts/characters/` | `draft-tags` **自动起草**的角色标签（关键词匹配天赋原文，分数一律 0.4），不参与配队；审核后挪到 `characters/` |
| `mvp_characters.yaml` | 第一版要覆盖的主C名单和录入进度 |
| `game/` | **自动生成**的游戏本体数据：每个角色一个文件（天赋原文与 10/13 级倍率、固有天赋、命座、90 级属性、官方一句话定位、技能里的名词解释、能开启的月曜/星烁反应、魔女的课业、`upgrades`：genshin-db 还没有的专项调整后天赋和命座，来自解包），`weapons.yaml`（90 级数值、R1/R5 效果），`artifacts.yaml`（2/4 件套效果），`glossary.yaml`（游戏内名词解释），`talent_multipliers.yaml`（天赋 1~15 级倍率，伤害计算用），`level_multiplier.yaml`（1~100 级等级系数） |
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

genshin-db 的天赋、命座文本不含专项调整（例如八重神子、莱欧斯利的星超导调整）和任务解锁的额外天赋，导入时从解包数据（AvatarSkillDepot、ProudSkill、AvatarTalent）补到角色的 `upgrades` 字段；天赋倍率表两边一致。官方一句话定位（`intro`）只有较新的角色有。

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
- **增益装备不重复**：`equipment_buffs.yaml` 里的套装和武器（翠绿之影、讨龙英杰谭、炉火融炼之心……）同一件一队只生效一次，所以一队只带一件。组队时每人按配装顺序挑不和队友撞的那套（阿罗夏「队里有人带炉火就带宗室」就是这样算出来的）；有人所有选项都撞时队伍不成立，所以只会带翠绿的砂糖和万叶不会同队。
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
