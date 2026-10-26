# genshin

原神配队与养成推荐引擎（自用）。给定一个角色，推荐合理的队伍和每个队员的养成方案；缺角色时找替补并说明理由；区分不同命座下的配队差异。

设计方案见项目文档《原神配队与养成 AI 助手：实现方案》。本仓库目前只有知识库格式和推荐引擎，大模型对话层和 B 站提取管线还没做。

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
| `game/` | **自动生成**的游戏本体数据：每个角色一个文件（天赋原文与 10/13 级倍率、固有天赋、命座、90 级属性、官方一句话定位、技能里的名词解释、能开启的月曜/星烁反应、魔女的课业），`weapons.yaml`（90 级数值、R1/R5 效果），`artifacts.yaml`（2/4 件套效果），`glossary.yaml`（游戏内名词解释） |
| `mechanics/` | 机制知识。`official_*.yaml` 是自动导入的游戏内教程和元素共鸣原文；`damage.yaml`（伤害公式）、`reactions.yaml`（元素反应，含月曜、星烁）、`team_systems.yaml`（月兆、魔导·秘仪、辉映·星烁、元素附着、ICD 等）是手写的，不确定的地方标了 `verified: false` |

`validate` 会核对 `characters/`、`builds/` 里的角色、武器、圣遗物中文名是否和游戏数据一致。

所有手写数据都带 `verified` 字段，`false` 表示草稿，还没按攻略核对。现有的那维莱特队数据就是草稿。

## 游戏数据

来源是 [genshin-db](https://github.com/theBowja/genshin-db)（npm 包，整理好的多语言数据）和 [AnimeGameData2](https://gitlab.com/Dimbreath/animegamedata2)（游戏解包数据，提供名词解释、教程、元素共鸣、文本表），两者都已更新到 7.1。下载内容缓存在 `.cache/gamedata/`，不进仓库。

```bash
python -m genshin_assistant import-gamedata              # 新版本上线、上游更新后重跑，生成 data/game/
python -m genshin_assistant game 桑多涅                   # 也可以查武器、圣遗物套装、名词（如 擢升）
python -m genshin_assistant game 水龙 --section cons      # 知识库里的别名也能用
python -m genshin_assistant enablers 星超导               # 哪些角色能为队伍开启这个反应
python -m genshin_assistant hints 沃雅妮莎                # 从天赋原文里找定位线索，录入 characters/ 时参考
```

已知缺口：genshin-db 不含「魔女的课业」解锁的额外天赋和技能变化，角色数据里只有从文本表找到的「魔导·秘仪」说明（`witch_quest` 字段）。官方一句话定位（`intro`）只有较新的角色有。

## 规则

- **命座**：`when: "cons >= 1"` 这类条件决定某个功能、需求或玩法是否生效。
- **核心功能**：候选人缺少槽位的 `core` 功能就不能入槽；槽位没人能填时整套模板判为不成立。
- **整队限制**：写在模板的 `constraints` 里，可用函数有 `count(元素)`、`max_count([元素...])`、`has_tag(标签)`、`has_char(角色)`，例如 `count('anemo') == 2 and max_count(['pyro', 'cryo', 'electro', 'hydro']) == 2`。
- **替补**：候选名单之外、满足全部核心功能的角色也会被自动考虑（基础分 0.5）；`substitutions` 里写的攻略结论会作为替补理由优先展示。
