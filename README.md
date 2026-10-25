# genshin

原神配队与养成推荐引擎（自用）。给定一个角色，推荐合理的队伍和每个队员的养成方案；缺角色时找替补并说明理由；区分不同命座下的配队差异。

设计方案见项目文档《原神配队与养成 AI 助手：实现方案》。本仓库目前有知识库格式、推荐引擎和 B 站攻略提取管线，大模型对话层还没做。

## 用法

```bash
pip install -e ".[dev]"
python -m genshin_assistant validate                       # 校验知识库
python -m genshin_assistant recommend 水龙                  # 0 命，假定拥有全部角色
python -m genshin_assistant recommend 那维莱特 --cons 1 --missing 钟离,万叶
python -m genshin_assistant recommend 那维莱特 --own 芙宁娜,砂糖,白术 --json   # 输出给大模型用的 JSON
pytest
```

## 知识库（`data/`）

| 路径 | 内容 |
| --- | --- |
| `tags.yaml` | 功能标签词表，其他文件只能引用这里的标签 |
| `characters/` | 角色：元素、武器、玩法，每个玩法的 `provides`（提供的功能）和 `needs`（对队友的需求），都可以带 `when` 条件（如 `cons < 1`） |
| `teams/` | 队伍模板：槽位、槽位功能分级（`core` / `major` / `minor`）、候选人、整队限制、攻略给出的替补结论 |
| `builds/` | 养成方案：按角色 × 玩法 × 命座区间，武器、圣遗物、主副词条、充能区间、天赋优先级 |
| `mvp_characters.yaml` | 第一版要覆盖的主C名单和录入进度 |

所有数据都带 `verified` 字段，`false` 表示草稿，还没按攻略核对。现有的那维莱特队数据就是草稿。

## 规则

- **命座**：`when: "cons >= 1"` 这类条件决定某个功能、需求或玩法是否生效。
- **核心功能**：候选人缺少槽位的 `core` 功能就不能入槽；槽位没人能填时整套模板判为不成立。
- **整队限制**：写在模板的 `constraints` 里，可用函数有 `count(元素)`、`max_count([元素...])`、`has_tag(标签)`、`has_char(角色)`，例如 `count('anemo') == 2 and max_count(['pyro', 'cryo', 'electro', 'hydro']) == 2`。
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
