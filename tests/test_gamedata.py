"""游戏数据导入、查询、定位线索的测试。"""

from pathlib import Path

import pytest
import yaml

from genshin_assistant import load
from genshin_assistant.gamedata import load_gamedata
from genshin_assistant.gamedata.hints import suggest
from genshin_assistant.gamedata.importer import Importer
from genshin_assistant.gamedata.text import clean, format_talent_param, render_talent_label
from genshin_assistant.kb import DEFAULT_DATA_DIR, KnowledgeBaseError


def test_clean_strips_markup_and_fills_params():
    raw = "<color=#FFD780FF>{LINK#N11330001}解算{/LINK}</color>持续{0}秒，提升{PARAM#P1192101|1S100}%\\n第二行"
    assert clean(raw, params=["6"], proud={1192101: [0.15]}) == "解算持续6秒，提升15%\n第二行"
    assert clean("{PARAM#P9|1S100}%") == "?%"


@pytest.mark.parametrize("fmt,value,expected", [
    ("F1P", 1.508, "150.8%"), ("P", 2.95, "295%"), ("I", 60.0, "60"), ("F1", 4.0, "4.0"),
])
def test_format_talent_param(fmt, value, expected):
    assert format_talent_param(fmt, value) == expected


def test_render_talent_label_uses_level():
    params = {"param1": [1.0, 2.0, 3.0], "param2": [0.5, 0.5, 0.5]}
    assert render_talent_label("伤害|{param1:F1P}+{param2:P}", params, 2) == ("伤害", "200.0%+50%")


def fake_sources():
    stats = {"base": {"hp": 1000.0, "attack": 20.0, "defense": 50.0, "critrate": 0.05, "critdmg": 0.5},
             "curve": {"hp": "C", "attack": "C", "defense": "C"}, "specialized": "FIGHT_PROP_CRITICAL",
             "promotion": [{"maxlevel": 90, "hp": 100.0, "attack": 10.0, "defense": 5.0, "specialized": 0.192}]}
    char = {"id": 10000133, "name": "测试员", "title": "", "elementType": "ELEMENT_CRYO", "weaponType": "WEAPON_CLAYMORE",
            "rarity": 5, "affiliation": "愚人众", "region": "", "substatText": "暴击率"}
    talents = {
        "name": "测试员",
        "combat1": {"name": "普攻", "description": "挥砍。", "descriptionRaw": "挥砍。",
                    "attributes": {"labels": ["一段伤害|{param1:F1P}", "冷却时间|{param2:F1}秒"]}},
        "combat2": {"name": "战技", "description": "为队伍中附近的角色创造护盾。", "descriptionRaw": "{LINK#N1}解算{/LINK}"},
        "combat3": {"name": "爆发", "description": "造成伤害。", "descriptionRaw": ""},
        "passive1": {"name": "被动", "description": "队伍中的角色触发超导/冰元素扩散反应时，将转为触发星超导/星扩散反应。",
                     "descriptionRaw": ""},
    }
    level = [float(i) for i in range(1, 16)]
    gdb = {
        "data": {"ChineseSimplified": {"characters": {"tester": char}, "talents": {"tester": talents},
                                       "constellations": {"tester": {"c1": {"name": "一命", "description": "队伍中所有角色攻击力提升20%。",
                                                                            "descriptionRaw": ""}}},
                                       "weapons": {}, "artifacts": {}},
                 "English": {"characters": {"tester": {"name": "Tester"}}}},
        "stats": {"characters": {"tester": stats}, "talents": {"tester": {"combat1": {"param1": level, "param2": [4.0] * 15}}},
                  "weapons": {}},
        "curve": {"characters": {"90": {"C": 2.0}}, "weapons": {}},
        "version": {"characters": {"tester": "7.0"}, "weapons": {}, "artifacts": {}},
    }
    agd = {
        "textmap": {"1": "解算", "2": "<color=#FFD780FF>法洁欧</color>的模式。", "3": "测试员是一位冰元素输出型角色。"},
        "textmap_medium": {},
        "proud_skills": [],
        "hyperlinks": [{"id": 1, "nameTextMapHash": 1, "descTextMapHash": 2, "descParamList": [""]}],
        "tutorials": [],
        "resonance": [],
    }
    return gdb, agd


def test_importer_builds_character_record():
    gdb, agd = fake_sources()
    c = Importer(gdb, agd, "test").character("tester")
    assert c["zh"] == "测试员" and c["en"] == "Tester" and c["element"] == "cryo" and c["weapon"] == "claymore"
    assert c["stats_lv90"] == {"hp": 2100, "atk": 50, "def": 105, "ascension_stat": "暴击率", "ascension_value": "24.2%"}
    assert c["talents"]["normal_attack"]["scaling"] == {"一段伤害": "1000.0% / 1300.0%", "冷却时间": "4.0秒"}
    assert c["enables"] == ["stellar_superconduct", "stellar_swirl"]
    assert c["terms"] == [{"id": 1, "name": "解算", "text": "法洁欧的模式。"}]
    assert c["intro"] == "测试员是一位冰元素输出型角色。"


def test_hints_find_tags_with_constellation_condition():
    gdb, agd = fake_sources()
    hints = {h.tag: h for h in suggest(Importer(gdb, agd, "test").character("tester"))}
    assert hints["shield"].when is None
    assert hints["buff:atk_pct"].when == "cons >= 1"


# ---------- 仓库里已生成的数据 ----------

needs_data = pytest.mark.skipif(not (DEFAULT_DATA_DIR / "game").exists(), reason="data/game/ 未生成")


@needs_data
def test_generated_data_covers_newest_characters():
    gd = load_gamedata()
    for zh in ["桑多涅", "奥黛塔", "阿罗夏", "薇斯纳", "沃雅妮莎", "法尔伽"]:
        assert gd.character(zh)["talents"]
    assert gd.character("Sandrone")["zh"] == "桑多涅"
    assert "stellar_swirl" in gd.character("薇斯纳")["enables"]
    assert gd.character("法尔伽").get("witch_quest")
    assert gd.find("擢升")[0] == "term"


@needs_data
def test_mechanics_roles_use_known_tags():
    gd = load_gamedata()
    kb = load()
    tutorials = gd.mechanics["official_tutorials"]["tutorials"]
    for r in gd.mechanics["reactions"]["reactions"]:
        for role in r.get("roles", []):
            assert kb.tag_error(role) is None, (r["id"], role)
        if "tutorial" in r:
            assert r["tutorial"] in tutorials, r["id"]


@needs_data
def test_validate_rejects_unofficial_names(tmp_path: Path):
    import shutil

    shutil.copytree(DEFAULT_DATA_DIR, tmp_path / "data", ignore=shutil.ignore_patterns("bili"))
    path = tmp_path / "data" / "characters" / "neuvillette_team.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("zh: 枫原万叶", "zh: 万叶"), encoding="utf-8")
    with pytest.raises(KnowledgeBaseError, match="万叶"):
        load(tmp_path / "data")


@needs_data
def test_identity_tags_come_from_game_data():
    gd = load_gamedata()
    assert {t for t, _ in gd.identity_tags("桑多涅")} == {"enables:stellar_superconduct", "enables:stellar_swirl"}
    assert ("witch_arcana" in {t for t, _ in gd.identity_tags("砂糖")})
    assert ("moonsign" in {t for t, _ in gd.identity_tags("爱诺")})
    assert ("nightsoul" in {t for t, _ in gd.identity_tags("玛薇卡")})
    kb = load()
    tags = {g.tag for g in kb.characters["sandrone"].playstyles[0].provides}
    assert "enables:stellar_swirl" in tags


def test_drafts_use_valid_tags():
    from genshin_assistant.models import Character

    kb = load()
    for path in sorted((DEFAULT_DATA_DIR / "drafts" / "characters").glob("*.yaml")):
        c = Character(**yaml.safe_load(path.read_text(encoding="utf-8")))
        assert c.zh not in {k.zh for k in kb.characters.values()}, f"{path.name} 已在知识库里，删掉草稿"
        for g in c.playstyles[0].provides:
            assert kb.tag_error(g.tag) is None, (path.name, g.tag)


@needs_data
def test_build_names_and_weapon_types_are_checked():
    from genshin_assistant.kb import check_build_names
    from genshin_assistant.models import Build

    gd = load_gamedata()
    ok = Build(char="vesna", weapons=[{"id": "x", "zh": "蝶变", "rank": 1}],
               artifact_sets=[[{"set": "mixed", "zh": "攻击力+18%", "pieces": 2},
                               {"set": "mixed", "zh": "攻击力+18%", "pieces": 2}]],
               main_stats={}, substats=[])
    assert check_build_names([ok], gd) == []
    bad = Build(**{**ok.model_dump(), "weapons": [{"id": "x", "zh": "超越之匙", "rank": 1}]})
    assert "claymore" in check_build_names([bad], gd)[0]
    too_many = Build(**{**ok.model_dump(), "artifact_sets": [[{"set": "a", "zh": "血红之证", "pieces": 4},
                                                              {"set": "b", "zh": "风起之日", "pieces": 2}]]})
    assert "超过 5 件" in check_build_names([too_many], gd)[0]


@needs_data
def test_draft_builds_use_official_names():
    from genshin_assistant.kb import check_build_names, load_draft_builds

    kb = load()
    drafts = load_draft_builds()
    if not drafts:
        pytest.skip("drafts/builds/ 是空的（2026-10-27 全部挪进 builds/）")
    assert check_build_names(drafts, load_gamedata()) == []
    for b in drafts:
        assert b.char not in kb.characters, f"{b.char} 已在知识库里，配装挪到 builds/"
