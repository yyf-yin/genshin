"""抽取管线测试：用假的大模型，验证纠错、引用定位、名字归一、投票、和知识库对照、草稿输出。"""

from collections import Counter
from pathlib import Path

import pytest
import yaml

from genshin_assistant import load
from genshin_assistant.bili.api import Comment, SubtitleLine
from genshin_assistant.bili.harvest import Material, Target
from genshin_assistant.extract.compare import annotate, kb_not_mentioned
from genshin_assistant.extract.extractor import ExtractContext, extract_video
from genshin_assistant.extract.glossary import Glossary, Term, load_glossary
from genshin_assistant.extract.llm import DeepSeek
from genshin_assistant.extract.pipeline import extract_target
from genshin_assistant.extract.schema import Claim, Evidence, VideoClaims
from genshin_assistant.extract.vote import aggregate

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="module")
def kb():
    return load(DATA)


@pytest.fixture(scope="module")
def glossary(kb):
    return load_glossary(kb, DATA / "bili" / "glossary.yaml")


def material(bvid="BV1", lines=None, desc="", pinned=()):
    lines = lines or [(5, "大家好今天讲那位莱特"), (20, "队友首选芙宁娜 提供全队增伤"), (35, "没有万业的话可以用砂糖")]
    return Material(bvid=bvid, title="那维莱特配队", up="up", up_mid=1, pubdate=1730000000, duration=600, desc=desc,
                    pinned=[Comment("up", 1, t, tag="置顶") for t in pinned], subtitle_lan="ai-zh",
                    subtitles=[SubtitleLine(t, t + 5, s) for t, s in lines])


class FakeModel:
    model = "fake"

    def __init__(self, claims):
        self.claims = claims
        self.prompts = []

    def complete_json(self, system, user):
        self.prompts.append((system, user))
        return {"claims": self.claims}


def ev(quote, source="subtitle"):
    return [{"source": source, "quote": quote}]


def test_glossary_fixes_asr_homophones_but_spares_two_char_words(glossary):
    c = Counter()
    assert glossary.correct("那位莱特和万业，干预一下", c) == "那维莱特和万叶，干预一下"
    assert c[("那位莱特", "那维莱特")] == 1 and c[("万业", "万叶")] == 1


def test_glossary_two_char_homophone_opt_in():
    g = Glossary([Term("ganyu", "甘雨", "character", homophone=True)], {})
    assert g.correct("赶鱼的蓄力") == "甘雨的蓄力"


def test_glossary_resolves_aliases(glossary):
    assert glossary.resolve("水龙", "character").id == "neuvillette"
    assert glossary.resolve("宗室", "artifact").id == "noblesse_oblige"
    assert glossary.resolve("水龙", "weapon") is None


def test_extract_keeps_quoted_claims_with_timestamps_and_drops_fabricated(kb, glossary):
    model = FakeModel([
        {"kind": "teammate", "subject": "那维莱特", "data": {"char": "芙宁娜", "functions": ["team_dmg_buff"]},
         "evidence": ev("队友首选芙宁娜 提供全队增伤")},
        {"kind": "substitution", "subject": "neuvillette", "data": {"original": "万叶", "substitute": "砂糖"},
         "evidence": ev("没有万叶的话，可以用砂糖")},  # 标点不同、错字已纠正，也要能定位
        {"kind": "teammate", "subject": "neuvillette", "data": {"char": "钟离"},
         "evidence": ev("钟离是必带的")},  # 原文里没有：丢弃
        {"kind": "slot_function", "subject": "neuvillette", "inferred": True,
         "data": {"tag": "new:驱散", "level": "core"}, "evidence": ev("队友首选芙宁娜")},
        {"kind": "bogus", "subject": "x", "evidence": ev("队友首选芙宁娜")},
    ])
    ctx = ExtractContext(kb, glossary, "neuvillette", "那维莱特")
    vc = extract_video(material(), ctx, model)

    assert "那维莱特" in model.prompts[0][1] and "万叶" in model.prompts[0][1]  # 模型看到的是纠错后的字幕
    assert vc.corrections == {"那位莱特→那维莱特": 1, "万业→万叶": 1}
    kinds = [(c.kind, c.subject, c.data.get("char") or c.data.get("substitute") or c.data.get("tag")) for c in vc.claims]
    assert kinds == [("teammate", "neuvillette", "furina"), ("substitution", "neuvillette", "sucrose"),
                     ("slot_function", "neuvillette", "new:驱散")]
    assert [c.evidence[0].t for c in vc.claims] == [20, 35, 20]
    assert vc.claims[0].evidence[0].link() == "https://www.bilibili.com/video/BV1/?t=20"
    assert set(vc.claims[2].flags) == {"词表外功能：驱散", "模型推断，视频没直说"}
    assert [r["why"][:4] for r in vc.rejected] == ["引用在材", "格式不对"]


def test_quote_found_in_pinned_comment_has_no_timestamp(kb, glossary):
    model = FakeModel([{"kind": "teammate", "subject": "neuvillette", "data": {"char": "白术"},
                        "evidence": ev("没钟离用白术也行", source="subtitle")}])
    vc = extract_video(material(pinned=["文字版：没钟离用白术也行"]), ExtractContext(kb, glossary, "neuvillette", "那维莱特"), model)
    assert vc.claims[0].evidence[0].source == "pinned" and vc.claims[0].evidence[0].t is None


def claim(bvid, kind="teammate", stance="for", cons=None, **data):
    return Claim(kind=kind, subject="neuvillette", stance=stance, cons=cons, data=data, bvid=bvid,
                 evidence=[Evidence(quote="q", bvid=bvid)])


def test_aggregate_counts_videos_and_flags_disputes():
    videos = [
        VideoClaims(bvid="A", claims=[claim("A", char="furina"), claim("A", char="furina"),
                                      claim("A", "slot_function", tag="interruption_res", level="core", cons="cons < 1")]),
        VideoClaims(bvid="B", claims=[claim("B", char="furina"), claim("B", char="zhongli", stance="against"),
                                      claim("B", "slot_function", tag="interruption_res", level="major", cons="cons < 1")]),
        VideoClaims(bvid="C", claims=[claim("C", char="zhongli"), claim("C", char="zhongli"),
                                      claim("C", "slot_function", tag="interruption_res", level="minor", cons="cons >= 1")]),
    ]
    by = {(f.kind, f.data.get("char") or f.data.get("tag"), f.cons): f for f in aggregate(videos)}
    furina = by[("teammate", "furina", None)]
    assert (furina.support, furina.disputed, furina.review_flags) == (2, False, [])
    zhongli = by[("teammate", "zhongli", None)]
    assert zhongli.disputed and zhongli.review_flags[0].startswith("有争议")
    c0 = by[("slot_function", "interruption_res", "cons < 1")]  # 不同命座分开计票
    assert c0.mentioned == 2 and c0.disputed
    assert "功能分级和整队限制最容易推断错，必须人审" in c0.review_flags
    c1 = by[("slot_function", "interruption_res", "cons >= 1")]
    assert c1.value == "minor" and "只有 1 个视频提到" in c1.review_flags


def test_compare_against_hand_entered_kb(kb):
    videos = [VideoClaims(bvid="A", claims=[
        claim("A", char="furina"),
        claim("A", char="nahida"),
        claim("A", "slot_function", tag="team_dmg_buff", level="core"),
        claim("A", "slot_function", tag="grouping", level="minor"),
        claim("A", "substitution", original="kazuha", substitute="sucrose"),
        claim("A", "build", field="weapon", value="万世流涌大典", id="tome_of_the_eternal_flow", rank=1),
    ])]
    findings = aggregate(videos)
    annotate(findings, kb, "neuvillette")
    status = {(f.kind, f.data.get("char") or f.data.get("tag") or f.data.get("value") or f.data.get("substitute")): f.kb_status
              for f in findings}
    assert status[("teammate", "furina")] == "一致"
    assert status[("teammate", "nahida")] == "知识库没有"
    assert status[("slot_function", "team_dmg_buff")] == "一致"
    assert status[("slot_function", "grouping")] == "冲突（知识库是核心）"
    assert status[("substitution", "sucrose")] == "一致（同槽候选）"
    assert status[("build", "万世流涌大典")] == "一致"
    missing = kb_not_mentioned(findings, kb, "neuvillette")
    assert "队伍 neuvillette_hydro 槽位 flex 的候选 zhongli" in missing
    assert "neuvillette 的武器 试作金珀" in missing


def test_pipeline_writes_draft_and_never_touches_kb(tmp_path, kb, glossary):
    work = tmp_path / "work"
    for bvid in ("BV1", "BV2"):
        material(bvid).save(work / "bili" / "neuvillette" / f"{bvid}.json")
    model = FakeModel([{"kind": "teammate", "subject": "neuvillette", "data": {"char": "芙宁娜"},
                        "evidence": ev("队友首选芙宁娜")}])
    before = {p: p.read_bytes() for p in DATA.rglob("*.yaml")}
    yml, md = extract_target(kb, glossary, Target("neuvillette", "那维莱特"), model, work, log=lambda *_: None)
    doc = yaml.safe_load(yml.read_text(encoding="utf-8"))
    assert doc["summary"]["videos"] == 2 and doc["findings"][0]["support"] == "2/2"
    assert doc["findings"][0]["review"] == "pending" and doc["findings"][0]["kb"] == "一致"
    assert doc["findings"][0]["evidence"][0]["t"] == "00:20"
    assert "队友首选芙宁娜" in md.read_text(encoding="utf-8")
    assert (work / "bili" / "neuvillette" / "BV1.claims.json").exists()  # 抽取结果缓存，重跑不再调用模型
    extract_target(kb, glossary, Target("neuvillette", "那维莱特"), model, work, log=lambda *_: None)
    assert len(model.prompts) == 2
    assert before == {p: p.read_bytes() for p in DATA.rglob("*.yaml")}


def test_deepseek_request_shape_and_cache(tmp_path):
    sent = []

    def post(url, body, headers):
        sent.append((url, body, headers))
        return {"choices": [{"message": {"content": '{"claims": []}'}}]}

    ds = DeepSeek(api_key="k", cache_dir=tmp_path, post=post, base_url="https://api.deepseek.com")
    assert ds.complete_json("sys", "user") == {"claims": []}
    assert ds.complete_json("sys", "user") == {"claims": []}
    url, body, headers = sent[0]
    assert len(sent) == 1 and url == "https://api.deepseek.com/chat/completions"
    assert body["response_format"] == {"type": "json_object"} and headers["Authorization"] == "Bearer k"
