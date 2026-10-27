# 伤害循环（配队排序用）

`profiles/` 里每个角色一份「标准循环」：一轮（按 20 秒算）里放哪些技能、各打几下、触发什么反应，
以及给自己和队友的增益。推荐引擎用它算每支候选队伍的全队期望总伤害，按总伤害给队伍排序。
代码在 `src/genshin_assistant/damage/team.py`。

全部是 AI 草稿（verified: false），按天赋原文和常见手法估的次数，等实测或攻略核对。

```yaml
char: odette
playstyle: default
on_field_seconds: 3        # 一轮里站场几秒（只做说明）
talents: {战技: 10}        # 可选；默认按 builds 的天赋要点：要点 10 级、不用点 6 级、其余 9 级
hits:                      # 一轮里的伤害
  - {talent: 战技:技能伤害, count: 1}
  - {talent: 破晓终奏星超导, variant: 0, stellar: 星超导, count: 1}   # 技能直接造成的星烁/月曜伤害
  - {talent: 普攻:一段伤害, base_multiplier_from: 战技:强化斥逐拳伤害, count: 4}  # 倍率再乘另一行天赋的数值
  - {name: 固有天赋落雷, scaling: {atk: 0.8}, element: electro, count: 8}       # 天赋表里没有的伤害
reactions:                 # 一轮里触发的剧变/月曜/星烁反应；by: on_field 表示按站场角色的面板算
  - {reaction: 星超导, count: 10, by: on_field}
buffs:                     # 增益；属性名同伤害计算场景文件（中文），百分比写数字
  - source: 固有天赋
    to: all                # self（默认）/ all / others / on_field / off_field / tag:witch_arcana
    stats: {冰伤: 40}
    when: "reaction('superconduct')"   # 可用 count / has_tag / count_tag / has_char / reaction / cons
    uptime: 0.8
  - source: 小小的慧风
    to: others
    from_stat: {stat: em, ratio: 0.2, key: 元素精通}   # 按提供者面板算的增益，可加 cap
  - {source: 触媒置换术, to: all, group: swirl, option: cryo, stats: {...}}  # 同组只取一个 option，引擎挑最优
```

会给队友加成的装备（翠绿之影、讨龙、炉火……）不用在这里写，用 `equipment_buffs.yaml` 的 value；
风套的 group 是 `swirl`，option 是元素英文名，和角色自己写的 `group: swirl` 一起选。
