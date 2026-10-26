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
from genshin_assistant.extract.pipeline import PROMPT_VERSION
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
    assert c1.value == "minor" and c1.support == 1  # 单一来源只看 support，不算需审核


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
    m = material("BV1")
    m.subtitles[0].text += "（OCR 重读）"  # 字幕换了，旧的抽取结果作废
    m.save(work / "bili" / "neuvillette" / "BV1.json")
    extract_target(kb, glossary, Target("neuvillette", "那维莱特"), model, work, log=lambda *_: None)
    assert len(model.prompts) == 3
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


def test_off_topic_video_yields_no_claims(kb, glossary):
    model = FakeModel([{"kind": "teammate", "subject": "neuvillette", "data": {"char": "芙宁娜"},
                        "evidence": ev("队友首选芙宁娜")}])
    model.complete_json = lambda system, user: {"relevant": False, "claims": model.claims}
    vc = extract_video(material(), ExtractContext(kb, glossary, "neuvillette", "那维莱特"), model)
    assert vc.claims == [] and vc.rejected[0]["why"].startswith("模型判断不是")


def test_main_stat_names_are_normalized_before_comparing(kb, glossary):
    model = FakeModel([
        {"kind": "build", "subject": "neuvillette", "data": {"field": "sands", "value": "生命"}, "evidence": ev("队友首选芙宁娜")},
        {"kind": "build", "subject": "neuvillette", "data": {"field": "goblet", "value": "攻击"}, "evidence": ev("大家好今天讲")},
    ])
    vc = extract_video(material(), ExtractContext(kb, glossary, "neuvillette", "那维莱特"), model)
    findings = aggregate([vc])
    annotate(findings, kb, "neuvillette")
    assert {f.data["field"]: f.kb_status for f in findings} == {"sands": "一致", "goblet": "冲突（知识库：hydro_dmg）"}


def test_long_subtitles_are_chunked_and_truncated_chunks_split(kb, glossary):
    from genshin_assistant.extract.llm import LLMTruncated
    lines = [(i * 10, f"第{i}句 队友首选芙宁娜{i:03d}") for i in range(100)]

    class Truncating(FakeModel):
        def complete_json(self, system, user):
            self.prompts.append((system, user))
            n = user.count("队友首选芙宁娜")
            if n > 50:
                raise LLMTruncated("截断")
            first = next(s for t, s in lines if s.split(" ")[1] in user)
            return {"claims": [{"kind": "teammate", "subject": "neuvillette", "data": {"char": "芙宁娜"},
                                "evidence": ev(first.split(" ")[1])}]}

    model = Truncating([])
    vc = extract_video(material(lines=lines, desc="简介"), ExtractContext(kb, glossary, "neuvillette", "那维莱特"),
                       model, chunk_chars=100000)
    assert len(model.prompts) == 3  # 整段被截断 → 对半拆成两段
    assert all("【简介 desc】" in u for _, u in model.prompts)  # 拆开的两半仍属于第 1 段，都带简介
    assert len(vc.claims) == 2 and {c.evidence[0].t for c in vc.claims} == {0, 500}


def test_chunk_lines_respects_budget():
    from genshin_assistant.extract.extractor import chunk_lines
    chunks = chunk_lines([(i, "字" * 10) for i in range(10)], budget=40)
    assert [len(c) for c in chunks] == [2, 2, 2, 2, 2]  # 每行按字数加 8 个字的时间戳计


def test_pipeline_skips_video_when_model_fails(tmp_path, kb, glossary):
    from genshin_assistant.extract.llm import LLMError
    work = tmp_path / "work"
    material("BV1").save(work / "bili" / "neuvillette" / "BV1.json")

    class Broken(FakeModel):
        def complete_json(self, system, user):
            raise LLMError("坏了")

    logs = []
    yml, _ = extract_target(kb, glossary, Target("neuvillette", "那维莱特"), Broken([]), work, log=logs.append)
    assert yaml.safe_load(yml.read_text(encoding="utf-8"))["summary"]["videos"] == 0
    assert any("抽取失败" in x for x in logs) and not (work / "bili" / "neuvillette" / "BV1.claims.json").exists()


def test_cached_claims_are_renormalized_with_current_glossary(tmp_path, kb, glossary):
    work = tmp_path / "work"
    d = work / "bili" / "neuvillette"
    material("BV1").save(d / "BV1.json")
    stale = VideoClaims(bvid="BV1", source_hash=f"{material('BV1').content_hash()}-{PROMPT_VERSION}", claims=[Claim(kind="teammate", subject="neuvillette", bvid="BV1",
                                                   data={"char": "new:莫娜"}, flags=["未知角色：new:莫娜"],
                                                   evidence=[Evidence(quote="队友首选芙宁娜", bvid="BV1", t=20)])])
    (d / "BV1.claims.json").write_text(stale.model_dump_json(), encoding="utf-8")
    yml, _ = extract_target(kb, glossary, Target("neuvillette", "那维莱特"), FakeModel([]), work, log=lambda *_: None)
    f = yaml.safe_load(yml.read_text(encoding="utf-8"))["findings"][0]
    assert f["data"]["char"] == "mona" and f["flags"] == []


def test_alternative_main_stat_is_not_a_conflict(kb):
    videos = [VideoClaims(bvid="A", claims=[claim("A", "build", field="circlet", value="生命", id="hp%", rank=2),
                                            claim("A", "build", field="goblet", value="生命", id="hp%")])]
    findings = aggregate(videos)
    annotate(findings, kb, "neuvillette")
    status = {f.data["field"]: f.kb_status for f in findings}
    assert status["circlet"].startswith("需对照（视频给的是备选")
    assert status["goblet"].startswith("冲突")


def test_teammates_released_later_are_counted_only_against_newer_videos():
    from datetime import date
    old = [VideoClaims(bvid=b, published="2026-02-20", claims=[
        claim(b, char="bennett", functions=["healer", "pyro_aura"]), claim(b, char="durin", functions=["shred"]),
        claim(b, char="venti", functions=["grouping"])]) for b in ("A", "B", "C")]
    new = [VideoClaims(bvid=b, published="2026-06-01", claims=[
        claim(b, char="nike", functions=["healer", "pyro_aura"]), claim(b, char="durin", functions=["shred"])])
        for b in ("D", "E", "F")]
    pre = [VideoClaims(bvid="P", published="2026-04-20", claims=[claim("P", char="nike", functions=["pyro_aura"])])]
    by = {f.data["char"]: f for f in aggregate(old + pre + new, releases={"nike": date(2026, 5, 14)},
                                               names={"nike": "妮可", "bennett": "班尼特"})}
    nike, bennett, durin, venti = by["nike"], by["bennett"], by["durin"], by["venti"]
    # 提前一个版本讲妮可的视频也算，分子分母同一个窗口
    assert (nike.eligible_support, nike.eligible) == (4, 4) and "妮可 2026-05-14 上线" in nike.eligible_note
    assert bennett.review_flags[0].startswith("可能已被 妮可 取代") and "healer、pyro_aura" in bennett.review_flags[0]
    assert not durin.review_flags and durin.dates == ("2026-02-20", "2026-06-01")  # 新视频也推荐，不算过时
    assert not venti.review_flags  # 功能不重叠，不算被取代
    few = aggregate(old + new[:2], releases={"nike": date(2026, 5, 14)})  # 新视频太少，不下结论
    assert not any(f.review_flags for f in few if f.data.get("char") == "bennett")



def test_findings_older_than_a_rework_are_flagged():
    from datetime import date
    videos = [VideoClaims(bvid="A", published="2025-01-01", claims=[claim("A", char="venti")]),
              VideoClaims(bvid="B", published="2026-06-01", claims=[claim("B", char="furina")])]
    by = {f.data["char"]: f for f in aggregate(videos, names={"venti": "温迪"},
                                               reworks={"venti": [(date(2026, 3, 1), "月之三")]})}
    assert by["venti"].review_flags[0] == "可能已过时：支持它的视频都早于 温迪 的加强（月之三，2026-03-01）"
    assert by["furina"].review_flags == []


def test_releases_file_adds_names_dates_and_reworks(tmp_path, kb):
    from datetime import date
    p = tmp_path / "releases.yaml"
    p.write_text("characters:\n  - {id: nike, zh: 妮可, since: 2026-05-20}\n  - {id: neuvillette, zh: 那维莱特, since: 2023-11-08}\n"
                 "reworks:\n  - {version: 月之三, date: 2026-03-01, characters: [venti]}\n", encoding="utf-8")
    g = load_glossary(kb, None, p)
    assert g.resolve("妮可").id == "nike" and g.releases()["nike"] == date(2026, 5, 20)
    assert g.releases()["neuvillette"] == date(2023, 11, 8)  # 知识库已有的角色也补上日期
    assert g.reworks == {"venti": [(date(2026, 3, 1), "月之三")]}


def test_not_superseded_when_still_recommended_in_a_playstyle_or_only_partly_covered():
    from datetime import date
    old = [VideoClaims(bvid=b, published="2026-02-20", claims=[
        claim(b, char="venti", functions=["energy_battery", "grouping"]),
        claim(b, char="fischl", functions=["energy_battery", "sub_dps"])]) for b in "ABC"]
    new = [VideoClaims(bvid=b, published="2026-06-01", claims=[claim(b, char="nike", functions=["energy_battery"])])
           for b in "DEF"]
    new[0].claims.append(Claim(kind="teammate", subject="neuvillette", playstyle="水伤", data={"char": "fischl",
                         "functions": ["sub_dps"]}, bvid="D", evidence=[Evidence(quote="q", bvid="D")]))
    by = {(f.data["char"], f.playstyle): f for f in aggregate(old + new, releases={"nike": date(2026, 5, 14)})}
    assert not by[("venti", None)].review_flags  # 妮可只顶得上充能，顶不上聚怪
    assert not by[("fischl", None)].review_flags  # 新视频在别的玩法里还推荐菲谢尔


def test_trailing_extra_brackets_from_the_model_are_tolerated():
    from genshin_assistant.extract.llm import LLMError, _parse_json
    assert _parse_json('{"claims": [{"q": 1}]}]}', "stop") == {"claims": [{"q": 1}]}
    with pytest.raises(LLMError):
        _parse_json('{"claims": []}, {"x": 1}', "stop")


def test_a_newcomer_backed_by_one_video_supersedes_nobody():
    from datetime import date
    old = [VideoClaims(bvid=b, published="2025-01-01", claims=[claim(b, char="diona", functions=["shield"])]) for b in "AB"]
    new = [VideoClaims(bvid=b, published="2026-06-01", claims=[claim(b, char="furina")]) for b in "CDE"]
    new[0].claims.append(claim("C", char="linnea", functions=["shield"]))
    by = {f.data["char"]: f for f in aggregate(old + new, releases={"linnea": date(2026, 4, 2)})}
    assert not by["diona"].review_flags


def test_teams_of_other_main_dps_are_compared_with_their_own_templates(kb):
    other = Claim(kind="teammate", subject="ayaka", stance="for", data={"char": "woyanisha"}, bvid="A",
                  evidence=[Evidence(quote="q", bvid="A")])
    findings = aggregate([VideoClaims(bvid="A", claims=[claim("A", char="furina"), other])])
    annotate(findings, kb, "neuvillette")
    status = {f.subject: f.kb_status for f in findings}
    assert status["neuvillette"] == "一致" and status["ayaka"] == "知识库没有（没有以 ayaka 为核心的队伍模板）"


def test_cores_report_gathers_teams_from_every_guide(tmp_path, kb, glossary):
    from genshin_assistant.extract.cores import by_core, write_cores_report
    bili = tmp_path / "bili"
    for target, bvid, subject in (("woyanisha", "BV1", "ayaka"), ("vesna", "BV2", "ayaka"), ("vesna", "BV3", "vesna")):
        d = bili / target
        d.mkdir(parents=True, exist_ok=True)
        vc = VideoClaims(bvid=bvid, published="2026-09-20", claims=[
            Claim(kind="teammate", subject=subject, data={"char": "woyanisha"}, bvid=bvid, evidence=[Evidence(quote="q", bvid=bvid)]),
            Claim(kind="build", subject="woyanisha", data={"field": "weapon", "value": "x"}, bvid=bvid,
                  evidence=[Evidence(quote="q", bvid=bvid)])])
        (d / f"{bvid}.claims.json").write_text(vc.model_dump_json(), encoding="utf-8")
    targets = [Target("woyanisha", "沃雅妮莎"), Target("vesna", "薇斯纳")]
    report = by_core(kb, glossary, targets, bili)
    assert set(report) == {"ayaka", "vesna"}  # 养成类结论不进按主 C 汇总
    (f, src, n), = report["ayaka"]
    assert (f.support, n, src) == (2, 2, ["沃雅妮莎", "薇斯纳"])
    text = write_cores_report(tmp_path / "out", report, {"ayaka": "神里绫华"}).read_text(encoding="utf-8")
    assert "## 神里绫华（ayaka）" in text and "来自：沃雅妮莎、薇斯纳 的攻略" in text
