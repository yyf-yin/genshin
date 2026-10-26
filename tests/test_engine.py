"""引擎测试。用虚构角色构造知识库，只验证规则行为，不依赖真实数据是否准确。"""

from pathlib import Path

import pytest
import yaml

from genshin_assistant import Engine, Owned, load
from genshin_assistant.conditions import ConditionError, evaluate
from genshin_assistant.kb import KnowledgeBaseError

TAGS = ["main_dps", "sub_dps", "team_dmg_buff", "elemental_shred", "grouping",
        "shield", "healer", "interruption_res", "cryo_application"]


def char(cid, element, provides, needs=(), playstyles=None):
    return {
        "id": cid, "zh": cid, "element": element, "weapon": "sword", "rarity": 5,
        "playstyles": playstyles or [{"id": "default", "provides": list(provides), "needs": list(needs)}],
    }


def make_kb(tmp_path: Path, characters, teams, builds=(), extra_tags=()):
    (tmp_path / "characters").mkdir()
    (tmp_path / "teams").mkdir()
    (tmp_path / "builds").mkdir()
    (tmp_path / "tags.yaml").write_text(yaml.safe_dump({"tags": [{"id": t, "zh": t} for t in TAGS] + list(extra_tags)}), encoding="utf-8")
    (tmp_path / "characters" / "c.yaml").write_text(yaml.safe_dump_all(characters, allow_unicode=True), encoding="utf-8")
    (tmp_path / "teams" / "t.yaml").write_text(yaml.safe_dump_all(teams, allow_unicode=True), encoding="utf-8")
    if builds:
        (tmp_path / "builds" / "b.yaml").write_text(yaml.safe_dump_all(list(builds), allow_unicode=True), encoding="utf-8")
    return load(tmp_path)


def roster(*chars, **cons):
    r = {c: Owned() for c in chars}
    for c, n in cons.items():
        r[c] = Owned(cons=n)
    return r


# ---------- 命座：需求随命座出现或消失 ----------

@pytest.fixture
def cons_kb(tmp_path):
    chars = [
        char("dragon", "hydro", [{"tag": "main_dps"}],
             needs=[{"tag": "interruption_res", "weight": 0.5, "when": "cons < 1", "reason": "0 命易被打断"}]),
        char("rock", "geo", [{"tag": "shield"}, {"tag": "interruption_res"}]),
        char("bard", "anemo", [{"tag": "team_dmg_buff"}]),
    ]
    team = {
        "id": "dragon_team", "zh": "龙队", "core": {"char": "dragon"},
        "slots": [
            {"id": "dps", "fixed": {"char": "dragon"}},
            {"id": "flex", "functions": [{"tag": "team_dmg_buff", "level": "minor", "weight": 0.2}],
             "candidates": [{"char": "bard", "score": 1.0}, {"char": "rock", "score": 0.8}]},
        ],
    }
    return make_kb(tmp_path, chars, [team])


def test_c0_needs_interruption_res_provider(cons_kb):
    rec = Engine(cons_kb).recommend("dragon", roster("rock", "bard", dragon=0))
    assert [m.char for m in rec.teams[0].members] == ["dragon", "rock"]
    assert rec.teams[0].needs[0].met_by == ["rock"]


def test_c1_drops_the_need(cons_kb):
    rec = Engine(cons_kb).recommend("dragon", roster("rock", "bard", dragon=1))
    assert [m.char for m in rec.teams[0].members] == ["dragon", "bard"]
    assert rec.teams[0].needs == []


def test_unmet_need_is_penalised_not_fatal(cons_kb):
    rec = Engine(cons_kb).recommend("dragon", roster("bard", dragon=0))
    need = rec.teams[0].needs[0]
    assert need.met_by == [] and need.penalty == 0.5


# ---------- 功能分级：缺核心功能即淘汰 ----------

@pytest.fixture
def core_kb(tmp_path):
    chars = [
        char("star", "anemo", [{"tag": "main_dps"}]),
        char("ice_queen", "cryo", [{"tag": "cryo_application"}, {"tag": "team_dmg_buff"}, {"tag": "sub_dps"}]),
        char("ice_kid", "cryo", [{"tag": "cryo_application"}]),
        char("buffer", "pyro", [{"tag": "team_dmg_buff"}, {"tag": "sub_dps"}]),
    ]
    team = {
        "id": "star_team", "zh": "星队", "core": {"char": "star"},
        "slots": [
            {"id": "dps", "fixed": {"char": "star"}},
            {"id": "cryo", "functions": [
                {"tag": "cryo_application", "level": "core"},
                {"tag": "team_dmg_buff", "level": "major", "weight": 0.3},
                {"tag": "sub_dps", "level": "minor", "weight": 0.1}],
             "candidates": [{"char": "ice_queen", "score": 1.0}, {"char": "buffer", "score": 0.9}]},
        ],
        "substitutions": [{"original": "ice_queen", "substitute": "ice_kid", "reason": "攻略结论：同样能挂冰"}],
    }
    return make_kb(tmp_path, chars, [team])


def test_candidate_without_core_function_is_excluded(core_kb):
    rec = Engine(core_kb).recommend("star", roster("ice_kid", "buffer"))
    assert rec.teams[0].members[1].char == "ice_kid"
    assert any(e.char == "buffer" and "缺少核心功能" in e.reason for e in rec.excluded)


def test_substitution_diff_and_override(core_kb):
    diff = Engine(core_kb).recommend("star", roster("ice_kid")).teams[0].substitutions[0]
    assert (diff.preferred, diff.chosen, diff.preferred_owned) == ("ice_queen", "ice_kid", False)
    assert diff.kept == ["cryo_application"]
    assert set(diff.lost) == {"team_dmg_buff", "sub_dps"}
    assert diff.override_reason == "攻略结论：同样能挂冰"


def test_template_invalid_when_core_slot_cannot_be_filled(core_kb):
    rec = Engine(core_kb).recommend("star", roster("buffer"))
    assert rec.teams == []
    assert "cryo_application" in rec.invalid[0].reason


# ---------- 整队限制 ----------

@pytest.fixture
def constraint_kb(tmp_path):
    chars = [
        char("wolf", "anemo", [{"tag": "main_dps"}]),
        char("breeze", "anemo", [{"tag": "elemental_shred"}, {"tag": "team_dmg_buff"}]),
        char("leaf", "anemo", [{"tag": "elemental_shred"}, {"tag": "team_dmg_buff"}, {"tag": "grouping"}]),
        char("flame_a", "pyro", [{"tag": "elemental_shred"}, {"tag": "team_dmg_buff"}]),
        char("flame_b", "pyro", [{"tag": "sub_dps"}]),
        char("stone", "geo", [{"tag": "elemental_shred"}, {"tag": "team_dmg_buff"}, {"tag": "shield"}]),
    ]
    team = {
        "id": "wolf_team", "zh": "狼队", "core": {"char": "wolf"},
        "slots": [
            {"id": "dps", "fixed": {"char": "wolf"}},
            {"id": "anemo", "fixed": {"char": "breeze"}},
            {"id": "buff", "functions": [{"tag": "team_dmg_buff", "level": "core"}],
             "candidates": [{"char": "flame_a", "score": 1.0}, {"char": "leaf", "score": 0.9}, {"char": "stone", "score": 0.9}]},
            {"id": "sub", "functions": [{"tag": "sub_dps", "level": "minor", "weight": 0.2}],
             "candidates": [{"char": "flame_b", "score": 1.0}]},
        ],
        "constraints": [{
            "id": "two_two",
            "rule": "count('anemo') == 2 and max_count(['pyro', 'cryo', 'electro', 'hydro']) == 2",
            "reason": "需要双风 + 双同元素",
        }],
    }
    return make_kb(tmp_path, chars, [team])


def test_constraint_rejects_functionally_equal_substitutes(constraint_kb):
    rec = Engine(constraint_kb).recommend("wolf", roster("breeze", "leaf", "stone", "flame_b"))
    assert rec.teams == []
    reasons = {e.char: e.reason for e in rec.excluded}
    assert "双风" in reasons["leaf"] and "双风" in reasons["stone"]


def test_constraint_satisfied(constraint_kb):
    rec = Engine(constraint_kb).recommend("wolf", roster("breeze", "leaf", "stone", "flame_a", "flame_b"))
    assert [m.char for m in rec.teams[0].members] == ["wolf", "breeze", "flame_a", "flame_b"]
    assert len(rec.teams) == 1


# ---------- 玩法：按命座解锁 ----------

def test_playstyle_unlocked_by_constellation(tmp_path):
    chars = [char("cloud", "anemo", [], playstyles=[
        {"id": "support", "provides": [{"tag": "team_dmg_buff"}]},
        {"id": "main_dps", "when": "cons >= 6", "provides": [{"tag": "main_dps"}]},
    ])]
    team = {"id": "cloud_main", "zh": "主C队", "core": {"char": "cloud", "playstyle": "main_dps"},
            "slots": [{"id": "dps", "fixed": {"char": "cloud", "playstyle": "main_dps"}}]}
    kb = make_kb(tmp_path, chars, [team])
    eng = Engine(kb)
    assert eng.recommend("cloud", roster(cloud=0)).invalid
    assert eng.recommend("cloud", roster(cloud=6)).teams


# ---------- 养成方案 ----------

def test_build_prefers_owned_or_craftable_weapon(tmp_path):
    chars = [char("hero", "hydro", [{"tag": "main_dps"}])]
    build = {"char": "hero", "cons": "0-6",
             "weapons": [{"id": "sig", "zh": "专武", "rank": 1, "source": "limited_5star"},
                         {"id": "craft", "zh": "锻造", "rank": 3, "source": "craftable"}],
             "artifact_sets": [[{"set": "s", "zh": "套装", "pieces": 4}]],
             "main_stats": {"sands": ["hp%"]}, "substats": ["crit_rate"]}
    kb = make_kb(tmp_path, chars, [], [build])
    eng = Engine(kb)
    assert eng.build("hero", "default", 0, owned_weapons=set())["recommended_weapon"]["id"] == "craft"
    assert eng.build("hero", "default", 0, owned_weapons={"sig"})["recommended_weapon"]["id"] == "sig"


# ---------- 角色自带的配队偏好：avoids、team_rules、元素需求、站场冲突 ----------

def pref_team(core, flex):
    return {
        "id": f"{core}_team", "zh": "测试队", "core": {"char": core},
        "slots": [{"id": "dps", "fixed": {"char": core}},
                  {"id": "a", "candidates": [{"char": c} for c in flex]},
                  {"id": "b", "candidates": [{"char": c} for c in flex]}],
    }


def test_avoid_penalises_teammate_with_tag(tmp_path):
    chars = [
        char("duke", "cryo", [{"tag": "main_dps"}],
             playstyles=[{"id": "default", "provides": [{"tag": "main_dps"}],
                          "avoids": [{"tag": "healer", "weight": 0.4, "reason": "要低血线"}]}]),
        char("nurse", "hydro", [{"tag": "healer"}]),
        char("bard", "anemo", [{"tag": "team_dmg_buff"}]),
        char("rock", "geo", [{"tag": "shield"}]),
    ]
    kb = make_kb(tmp_path, chars, [pref_team("duke", ["nurse", "bard", "rock"])])
    rec = Engine(kb).recommend("duke")
    assert {m.char for m in rec.teams[0].members} == {"duke", "bard", "rock"}
    with_nurse = next(t for t in rec.teams if "nurse" in {m.char for m in t.members})
    assert with_nurse.conflicts[0].kind == "avoid" and with_nurse.conflicts[0].penalty == 0.4


def test_hard_rule_invalidates_and_soft_rule_penalises(tmp_path):
    rules = [
        {"id": "need_x", "hard": True, "rule": "count_any(['pyro', 'hydro']) >= 1", "reason": "没有火水队友"},
        {"id": "two_anemo", "weight": 0.3, "rule": "count('anemo') >= 2", "reason": "双风加成"},
    ]
    chars = [
        char("knight", "anemo", [], playstyles=[{"id": "default", "provides": [{"tag": "main_dps"}], "team_rules": rules}]),
        char("bard", "anemo", [{"tag": "team_dmg_buff"}]),
        char("rock", "geo", [{"tag": "shield"}]),
        char("flame", "pyro", [{"tag": "sub_dps"}]),
    ]
    kb = make_kb(tmp_path, chars, [pref_team("knight", ["bard", "rock", "flame"])])
    rec = Engine(kb).recommend("knight")
    teams = {frozenset(m.char for m in t.members): t for t in rec.teams}
    assert frozenset({"knight", "bard", "rock"}) not in teams  # 违反硬规则
    assert teams[frozenset({"knight", "bard", "flame"})].conflicts == []
    soft = teams[frozenset({"knight", "rock", "flame"})].conflicts
    assert [(c.kind, c.penalty) for c in soft] == [("rule", 0.3)]


def test_element_need_with_count_excludes_self(tmp_path):
    chars = [
        char("frost", "cryo", [], playstyles=[{"id": "default", "provides": [{"tag": "main_dps"}],
                                               "needs": [{"tag": "element:cryo", "weight": 0.4, "count": 2}]}]),
        char("ice", "cryo", [{"tag": "cryo_application"}]),
        char("rock", "geo", [{"tag": "shield"}]),
    ]
    kb = make_kb(tmp_path, chars, [pref_team("frost", ["ice", "rock"])],
                 extra_tags=[{"id": "element", "zh": "元素", "param": "element"}])
    need = Engine(kb).recommend("frost").teams[0].needs[0]
    assert need.met_by == ["ice"] and need.penalty == 0.2 and need.tag_zh == "元素：冰"


def test_two_on_field_characters_conflict(tmp_path):
    on = lambda cid, el: char(cid, el, [], playstyles=[{"id": "default", "field": "on_field", "provides": [{"tag": "main_dps"}]}])
    chars = [on("a1", "pyro"), on("a2", "hydro"), char("bard", "anemo", [{"tag": "team_dmg_buff"}]),
             char("rock", "geo", [{"tag": "shield"}])]
    rec = Engine(make_kb(tmp_path, chars, [pref_team("a1", ["a2", "bard", "rock"])])).recommend("a1")
    assert "a2" not in {m.char for m in rec.teams[0].members}
    clash = next(t for t in rec.teams if "a2" in {m.char for m in t.members})
    assert [c.kind for c in clash.conflicts] == ["field"]


def test_param_tags_are_validated(tmp_path):
    tags = [{"id": "element_dmg_buff", "zh": "元素增伤", "param": "element"}]
    with pytest.raises(KnowledgeBaseError, match="未知元素"):
        make_kb(tmp_path, [char("x", "geo", [{"tag": "element_dmg_buff:wood"}])], [], extra_tags=tags)


def test_param_tag_requires_param(tmp_path):
    tags = [{"id": "element_dmg_buff", "zh": "元素增伤", "param": "element"}]
    with pytest.raises(KnowledgeBaseError, match="需要参数"):
        make_kb(tmp_path, [char("x", "geo", [{"tag": "element_dmg_buff"}])], [], extra_tags=tags)


# ---------- 知识库与条件求值 ----------

def test_unknown_tag_fails_validation(tmp_path):
    with pytest.raises(KnowledgeBaseError, match="未定义的标签"):
        make_kb(tmp_path, [char("x", "geo", [{"tag": "no_such_tag"}])], [])


@pytest.mark.parametrize("expr", ["__import__('os')", "cons.__class__", "(lambda: 1)()"])
def test_condition_rejects_unsafe_code(expr):
    with pytest.raises(ConditionError):
        evaluate(expr, {"cons": 0})


def test_repository_data_is_valid():
    load()
