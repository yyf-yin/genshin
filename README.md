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

所有数据都带 `verified` 字段，`false` 表示草稿，还没按攻略核对。现有的那维莱特队数据就是草稿。

## 规则

- **命座**：`when: "cons >= 1"` 这类条件决定某个功能、需求或玩法是否生效。
- **核心功能**：候选人缺少槽位的 `core` 功能就不能入槽；槽位没人能填时整套模板判为不成立。
- **整队限制**：写在模板的 `constraints` 里，可用函数有 `count(元素)`、`max_count([元素...])`、`has_tag(标签)`、`has_char(角色)`，例如 `count('anemo') == 2 and max_count(['pyro', 'cryo', 'electro', 'hydro']) == 2`。
- **替补**：候选名单之外、满足全部核心功能的角色也会被自动考虑（基础分 0.5）；`substitutions` 里写的攻略结论会作为替补理由优先展示。
