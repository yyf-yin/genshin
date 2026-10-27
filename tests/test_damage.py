"""伤害计算：公式各乘区、天赋倍率解析、场景端到端。"""

from __future__ import annotations

import pytest

from genshin_assistant.damage import calculate
from genshin_assistant.damage import formulas as f
from genshin_assistant.damage.stats import parse_value, set_2pc_stats, stat_key
from genshin_assistant.damage.talents import Talent, find_talent, load_talents
from genshin_assistant.gamedata import load_gamedata
from genshin_assistant.gamedata.importer import level_multipliers, talent_multipliers


@pytest.fixture(scope="module")
def gd():
    return load_gamedata()


# ---------- 公式 ----------

def test_defense_multiplier():
    assert f.defense_multiplier(90, 100) == pytest.approx(190 / 390)
    assert f.defense_multiplier(90, 90) == pytest.approx(0.5)
    # 减防 30%、无视防御 50%：敌人防御项 ×0.7×0.5
    assert f.defense_multiplier(90, 100, 0.3, 0.5) == pytest.approx(190 / (190 + 200 * 0.35))


@pytest.mark.parametrize("res, expected", [(0.1, 0.9), (-0.3, 1.15), (0.0, 1.0), (0.8, 1 / 4.2)])
def test_resistance_multiplier(res, expected):
    assert f.resistance_multiplier(res) == pytest.approx(expected)


def test_crit_capped_at_100_percent():
    assert f.crit_multipliers(1.3, 1.0) == (2.0, 2.0)
    assert f.crit_multipliers(0.5, 1.0) == (2.0, 1.5)


def test_em_bonus():
    assert f.em_bonus(100, "amplifying") == pytest.approx(2.78 * 100 / 1500)
    assert f.em_bonus(1000, "transformative") == pytest.approx(16 / 3)
    # 月曜：攻略常引用的 500/1000/1500 精通 → +120%/+200%/+257%
    assert f.em_bonus(500, "lunar") == pytest.approx(1.2)
    assert f.em_bonus(1000, "lunar") == pytest.approx(2.0)
    assert f.em_bonus(1500, "lunar") == pytest.approx(2.571, abs=1e-3)


def test_reactions_at_level_90(gd):
    lv = gd.level_multiplier(90)
    assert lv == pytest.approx(1446.85, abs=0.01)
    assert f.amplifying_multiplier("vaporize", "hydro", 0) == 2.0
    assert f.amplifying_multiplier("melt", "cryo", 0) == 1.5
    with pytest.raises(ValueError):
        f.amplifying_multiplier("melt", "hydro", 0)
    assert f.transformative_base("overloaded", lv, 0) == pytest.approx(2.75 * 1446.85, abs=0.1)
    assert f.additive_damage("spread", lv, 0) == pytest.approx(1.25 * 1446.85, abs=0.1)


def test_level_multiplier_table_is_complete(gd):
    assert sorted(gd.level_multipliers) == list(range(1, 101))
    assert gd.level_multiplier(80) == pytest.approx(1077.44, abs=0.01)


def test_lunar_team_total():
    assert f.lunar_team_total([40, 100, 20, 80]) == pytest.approx(100 + 40 + 40 / 12 + 20 / 12)


# ---------- 属性 ----------

def test_stat_aliases_and_values():
    assert stat_key("水伤") == "hydro_dmg"
    assert stat_key("重击增伤") == "charged_dmg"
    assert stat_key("蒸发加成") == "vaporize_bonus"
    assert stat_key("月感电基础伤害提升") == "lunar_charged_base_bonus"
    with pytest.raises(KeyError):
        stat_key("攻速")
    assert parse_value("cr", 20) == 0.2
    assert parse_value("cr", "20%") == 0.2
    assert parse_value("em", 80) == 80


def test_set_2pc():
    assert set_2pc_stats("获得15%水元素伤害加成。") == {"hydro_dmg": 0.15}
    assert set_2pc_stats("普通攻击与重击造成的伤害提高15%。") == {"normal_dmg": 0.15, "charged_dmg": 0.15}
    assert set_2pc_stats("装备者处于夜魂加持状态，并且在场上时，造成的伤害提高15%。") is None


# ---------- 天赋倍率 ----------

def _t(template, **params):
    return Talent("normal_attack", "测试", template, {k: [v] * 15 for k, v in params.items()})


def test_talent_parsing():
    h = _t("{param1:F1P}攻击力+{param2:F1P}元素精通", param1=1.0, param2=2.0).hits(10)
    assert len(h) == 1 and h[0].scaling == {"atk": 1.0, "em": 2.0}
    assert len(_t("{param1:F1P}+{param2:F1P}", param1=1.0, param2=0.5).hits(10)) == 2
    assert len(_t("({param1:F1P}攻击力+{param2:F1P}元素精通)*3", param1=1.0, param2=2.0).hits(10)) == 3
    # 「A+B防御力」两段都按防御力
    assert [x.scaling for x in _t("{param1:F1P}+{param2:F1P}防御力", param1=1.0, param2=0.5).hits(10)] == \
        [{"def": 1.0}, {"def": 0.5}]
    # 低空/高空两种情况
    t = _t("{param1:P}/{param2:P}", param1=1.0, param2=2.0)
    assert t.hits(10, 1)[0].scaling == {"atk": 2.0}
    # 「/层」只是单位
    assert len(_t("{param1:F2P}生命值上限/层", param1=0.01).variants()) == 1
    # 固定值加在同一段
    h = _t("{param1:F2P}生命值上限+{param2:I}", param1=0.1, param2=500).hits(10)
    assert h[0].scaling == {"hp": 0.1} and h[0].flat == 500
    assert _t("{param1:F1P}秒", param1=1.0).hits(10) == []


def test_real_talents(gd):
    talents = load_talents(gd.talent_multipliers["neuvillette"])
    t = find_talent(talents, "衡平推裁")
    assert t.hits(10)[0].scaling == {"hp": pytest.approx(0.1447, abs=1e-4)}  # 游戏内 10 级 14.47%
    assert t.hits(13)[0].scaling == {"hp": pytest.approx(0.1753, abs=1e-4)}
    with pytest.raises(KeyError):
        find_talent(talents, "技能伤害")  # 战技、爆发都有
    assert find_talent(talents, "爆发:技能伤害").slot == "burst"


def test_importer_helpers():
    talent = {"combat1": {"attributes": {"labels": ["一段伤害|{param1:F1P}", "冷却时间|{param2:F1}秒"]}}}
    params = {"combat1": {"param1": [0.5] * 15, "param2": [12.0] * 15}}
    out = talent_multipliers(talent, params)
    assert [r["name"] for r in out["normal_attack"]] == ["一段伤害"]
    assert level_multipliers([{"playerElementLevelCo": 1.0}, {"playerElementLevelCo": 17.165606}]) == {1: 17.1656}


# ---------- 端到端 ----------

BASE = {
    "character": "那维莱特",
    "talents": {"普攻": 10},
    "weapon": "万世流涌大典",
    "artifacts": {"sets": ["逐影猎人4"], "sands": "生命值%", "goblet": "水伤", "circlet": "暴击伤害",
                  "substats": {"暴击率": 30}},
    "buffs": [{"source": "测试", "stats": {"增伤": 50}}],
    "enemy": {"level": 100, "res": 10},
}


def test_neuvillette_end_to_end(gd):
    sc = dict(BASE, hits=[{"talent": "衡平推裁"}])
    r = calculate(gd, sc)
    hp = 14695 * (1 + 0.466) + 4780
    assert r.panel["生命值"] == pytest.approx(hp)
    assert r.panel["暴击率"] == pytest.approx(0.35)
    assert r.panel["暴击伤害"] == pytest.approx(0.884 + 0.882 + 0.622)
    h = r.hits[0]
    assert h.element == "hydro"
    dmg = 1 + 0.466 + 0.15 + 0.5  # 水伤杯 + 逐影 2 件套（重击）+ buff
    expected = 0.14467 * hp * dmg * (190 / 390) * 0.9
    assert h.non_crit == pytest.approx(expected, rel=1e-4)
    assert h.crit == pytest.approx(expected * (1 + 0.884 + 0.882 + 0.622), rel=1e-4)
    assert any("逐影猎人" in w for w in r.warnings)  # 4 件套要手写


def test_vaporize_and_reaction_entries(gd):
    sc = dict(BASE, hits=[
        {"talent": "灵息之刺", "reaction": "蒸发"},
        {"reaction": "超载"},
        {"reaction": "月感电"},
    ])
    r = calculate(gd, sc)
    vap, overload, lunar = r.hits
    assert any(z.name == "增幅反应区" and z.value == pytest.approx(2.0) for z in vap.zones)
    assert overload.non_crit == pytest.approx(2.75 * 1446.8535 * 0.9) and overload.crit == overload.non_crit
    assert lunar.non_crit == pytest.approx(1.8 * 1446.8535 * 0.9, rel=1e-6)
    assert lunar.crit > lunar.non_crit  # 月曜反应可以暴击


def test_direct_lunar_skips_defense_and_dmg_bonus(gd):
    sc = {"character": "菲林斯", "talents": {"爆发": 10}, "buffs": [{"source": "测试", "stats": {"增伤": 100}}],
          "hits": [{"talent": "最终段月感电伤害"}]}
    h = calculate(gd, sc).hits[0]
    names = [z.name for z in h.zones]
    assert "防御区" not in names and "增伤区" not in names
    atk = gd.character("菲林斯")["stats_lv90"]["atk"]
    mult = gd.talent_multipliers["flins"]["burst"][2]["params"]["param3"][9]
    assert h.non_crit == pytest.approx(mult * atk * 3.0 * 0.9, rel=1e-6)


def test_stellar_superconduct_reaction(gd):
    h = calculate(gd, dict(BASE, hits=[{"reaction": "星超导"}])).hits[0]
    assert h.element == "cryo"
    assert h.non_crit == pytest.approx(2.0 * 1446.8535 * 0.9, rel=1e-6)  # 系数按叠满的 2.0
    with pytest.raises(NotImplementedError):
        calculate(gd, dict(BASE, hits=[{"reaction": "星扩散"}]))  # 星扩散反应系数未知


# ---------- 队伍伤害 ----------

@pytest.fixture(scope="module")
def kb():
    from genshin_assistant.kb import load

    return load()


def _team(kb, names):
    from genshin_assistant.damage.team import MemberSpec
    from genshin_assistant.engine import Engine

    e = Engine(kb)
    kits = [e.kit(kb.resolve(n).id, "default", 0) for n in names]
    kits, why = e._assign_gear(kits, None)
    assert why is None
    return [MemberSpec(k.char, k.zh, k.element, k.playstyle, k.cons, set(k.provides), k.position or (None if i else "on_field"),
                       e._find_build(k.char, k.playstyle, k.cons), k.weapon_choice, k.artifact_choice,
                       [x for x in kb.equipment if x.zh in k.gear]) for i, k in enumerate(kits)]


def test_witch_buff_needs_matching_reaction(kb):
    from genshin_assistant.damage.team import team_damage

    d = team_damage(kb.game, _team(kb, ["莱欧斯利", "奥黛塔", "菲谢尔", "砂糖"]), kb.damage_profiles)
    # 菲谢尔的魔导·秘仪只在超载、感电时生效，冰雷队里没有；砂糖的魔导·秘仪（两名魔导角色）生效
    assert not any("菲谢尔·魔导" in b for b in d.buffs)
    assert any("砂糖·魔导·秘仪（小型风灵）" in b for b in d.buffs)


def test_viridescent_shreds_one_element_xilonen_shreds_all(kb):
    from genshin_assistant.damage.team import team_damage

    d = team_damage(kb.game, _team(kb, ["莱欧斯利", "奥黛塔", "菲谢尔", "砂糖"]), kb.damage_profiles)
    vv = [b for b in d.buffs if "翠绿之影" in b]
    assert len(vv) == 1 and d.choices == ["砂糖·swirl：cryo"]
    d2 = team_damage(kb.game, _team(kb, ["莱欧斯利", "奥黛塔", "菲谢尔", "希诺宁"]), kb.damage_profiles)
    shred = [b for b in d2.buffs if "音火锻淬" in b]
    assert any("冰" in b for b in shred) and any("雷" in b for b in shred) and not any("（水）" in b for b in shred)


def test_engine_ranks_by_team_damage(kb):
    from genshin_assistant.engine import Engine

    rec = Engine(kb).recommend("莱欧斯利", None, None, top_n=10)
    with_damage = [t for t in rec.teams if t.damage]
    assert with_damage
    best = max(t.damage["total"] for t in with_damage)
    for t in with_damage:
        assert t.score == pytest.approx(t.damage["total"] / best - t.penalty, abs=1e-3)
