"""单个视频的结构化抽取：纠错 → 组织材料 → DeepSeek 抽取 → 名字归一 → 校验标签 → 定位原文和时间戳。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from pydantic import ValidationError

from ..bili.harvest import Material
from ..kb import KnowledgeBase
from .glossary import Glossary
from .llm import JSONModel, LLMTruncated
from .schema import BUILD_FIELDS, Claim, Evidence, VideoClaims

SYSTEM_PROMPT = """你是原神攻略整理助手。从一个 B 站攻略视频的简介、置顶评论和字幕里，抽取关于核心角色「{core}」的结论，
输出 JSON：{{"relevant": true | false, "claims": [ ... ]}}。只抽视频里明确说了的内容，不要用你自己的游戏知识补充。
relevant：这是不是一个讲《原神》角色「{core}」的配队、养成或玩法的视频。别的游戏（例如碧蓝航线）、剧情、抽卡、外观视频写 false，
此时 claims 留空。

每条 claim 的字段：
- kind：下面七种之一
- subject：角色 id（见名词表；名词表里没有的角色直接写中文名）。队伍类结论（teammate、slot_function、team_constraint、
  substitution）写这支队伍的主 C：「{core}」自己当主 C 的队伍写「{core}」；视频里讲到「{core}」去别的主 C 队里当辅助
  （例如“绫华队里带她”），subject 写那个主 C，那支队的队友、功能需求、替代也照样抽，「{core}」本人作为 teammate 写进去
- cons：生效条件，用 "cons >= 1"、"cons < 2" 这种写法，指 subject 的命座；不分命座就写 null
- playstyle：玩法名（例如“月结晶”“蓄力”），视频没区分就写 null
- stance："for"（推荐/可以）或 "against"（明确说不推荐/不行）
- inferred：视频没直说、是你从上下文推断的，写 true
- data：按 kind 填，见下
- evidence：[{{"source": "subtitle" | "desc" | "pinned", "quote": "原文"}}]。quote 必须从材料里**逐字复制**一段连续文字（10 到 60 字），
  不要改写、不要拼接、不要带 [mm:ss] 时间戳。找不到能支撑的原文就不要输出这条 claim。

七种 kind 和 data：
1. teammate：推荐的队友。data = {{"char": 队友 id, "functions": [该队友在队里提供的功能标签]}}
2. slot_function：队伍某个位置需要的功能及其重要程度。data = {{"tag": 功能标签, "level": "core" | "major" | "minor", "slot_note": "位置说明"}}。
   core = 视频说没有就组不起来/必须要；major = 很重要、缺了明显变弱；minor = 有更好。视频没直说重要程度时 inferred 写 true
3. team_constraint：只能对整队判断的要求（元素数量、武器类型、必须有某机制等）。
   data = {{"description": "原话意思", "rule": "可选，用 count('anemo') == 2、max_count(['pyro','cryo']) == 2、has_tag('shield')、has_char('furina') 写的表达式"}}
4. substitution：没有 A 时用 B。data = {{"original": A 的 id, "substitute": B 的 id, "reason": "理由", "build_changes": ["其他队员配装的调整"]}}
5. cons_effect：命座带来的变化。subject 是拥有该命座的角色，cons 写条件。
   data = {{"effect": "变化说明", "tag": "可选，相关功能标签", "change": "provides" | "removes_need" | "adds_need" | "other"}}
6. build：养成建议。subject 是被养成的角色（可以是队友）。data = {{"field": {fields} 之一, "value": "中文名或数值", "rank": 推荐顺位（1 最好，可省略）}}
   一个武器/套装/词条一条 claim。视频说“某些情况下可以换成”的备选，rank 写 2 或更大，不要写 1。energy_recharge 的 value 写成 "120-140" 这种区间
7. playstyle：角色的不同玩法。data = {{"name": "玩法名", "when": "解锁条件，可选", "description": "说明"}}

功能标签只能从下表选，表里没有合适的就写 "new:中文说明"：
{tags}

角色名词表（id: 中文名（别名））：
{chars}

字幕是自动识别的，可能有错字；按意思理解，但 quote 仍然照抄原文。"""


@dataclass
class ExtractContext:
    kb: KnowledgeBase
    glossary: Glossary
    core_id: str
    core_zh: str


def _fmt(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def corrected_material(m: Material, g: Glossary) -> tuple[dict[str, str], list[tuple[float, str]], Counter]:
    """返回 ({source: 文本}, [(秒, 字幕行)], 纠错计数)。简介和评论是人打的字，只套错字表，不做同音纠错。"""
    counter: Counter = Counter()
    lines = [(s.start, g.correct(s.text, counter)) for s in m.subtitles]
    texts = {
        "desc": g.correct(m.desc, counter, asr=False),
        "pinned": "\n\n".join(g.correct(c.text, counter, asr=False) for c in m.pinned),
        "subtitle": "\n".join(t for _, t in lines),
    }
    return texts, lines, counter


def build_user_prompt(m: Material, texts: dict[str, str], lines: list[tuple[float, str]],
                      part: tuple[int, int] = (1, 1)) -> str:
    parts = [f"视频：{m.title}", f"UP 主：{m.up}    发布：{m.published}    BV：{m.bvid}"]
    if part[0] == 1:
        parts.append("\n【简介 desc】\n" + (texts["desc"] or "（无）"))
        parts.append("\n【置顶评论 pinned】\n" + (texts["pinned"] or "（无）"))
    else:
        parts.append("\n（简介和置顶评论已在前面的分段处理过，这一段只看字幕）")
    label = f"（第 {part[0]}/{part[1]} 段）" if part[1] > 1 else ""
    parts.append(f"\n【字幕 subtitle】{label}\n" + ("\n".join(f"[{_fmt(t)}] {s}" for t, s in lines) or "（无字幕）"))
    return "\n".join(parts)


def chunk_lines(lines: list[tuple[float, str]], budget: int) -> list[list[tuple[float, str]]]:
    """按字数把字幕切段。长视频一次喂进去，抽出来的 JSON 会超过模型的输出上限被截断。"""
    chunks: list[list[tuple[float, str]]] = [[]]
    size = 0
    for line in lines:
        if chunks[-1] and size + len(line[1]) > budget:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line[1]) + 8
    return chunks


# 词条中文名 → 知识库配装里用的写法
STAT_IDS = {
    "生命": "hp%", "生命值": "hp%", "大生命": "hp%", "生命百分比": "hp%",
    "攻击": "atk%", "攻击力": "atk%", "大攻击": "atk%", "攻击百分比": "atk%",
    "防御": "def%", "防御力": "def%", "大防御": "def%",
    "精通": "em", "元素精通": "em", "充能": "energy_recharge", "元素充能效率": "energy_recharge", "充能效率": "energy_recharge",
    "暴击": "crit_rate", "暴击率": "crit_rate", "暴伤": "crit_dmg", "暴击伤害": "crit_dmg", "双暴": "crit",
    "治疗加成": "healing_bonus", "物伤": "physical_dmg", "物理伤害加成": "physical_dmg",
    **{f"{zh}伤": f"{en}_dmg" for zh, en in [("水", "hydro"), ("火", "pyro"), ("雷", "electro"), ("冰", "cryo"),
                                            ("风", "anemo"), ("岩", "geo"), ("草", "dendro")]},
    **{f"{zh}元素伤害加成": f"{en}_dmg" for zh, en in [("水", "hydro"), ("火", "pyro"), ("雷", "electro"), ("冰", "cryo"),
                                                  ("风", "anemo"), ("岩", "geo"), ("草", "dendro")]},
}


def _squash_stat(s: str) -> str:
    return "".join(s.split()).replace("杯", "").replace("沙", "").replace("头", "").removesuffix("%")


_KEEP = re.compile(r"[\w一-鿿]")


def _norm(s: str) -> tuple[str, list[int]]:
    """去掉空白和标点后的文本，以及每个字符在原文里的位置。"""
    out, pos = [], []
    for i, c in enumerate(s):
        if _KEEP.match(c):
            out.append(c.lower())
            pos.append(i)
    return "".join(out), pos


class Locator:
    """在材料里找引用原文；字幕里找到的，按所在行给出时间戳。"""

    def __init__(self, texts: dict[str, str], lines: list[tuple[float, str]]):
        self.norm = {k: _norm(v) for k, v in texts.items()}
        self.line_starts: list[tuple[int, float]] = []
        offset = 0
        for t, s in lines:
            self.line_starts.append((offset, t))
            offset += len(s) + 1

    def find(self, ev: Evidence) -> Evidence | None:
        q, _ = _norm(ev.quote)
        if len(q) < 4:
            return None
        order = [ev.source] + [s for s in ("subtitle", "pinned", "desc") if s != ev.source]
        for src in order:
            text, pos = self.norm.get(src, ("", []))
            i = text.find(q)
            if i < 0:
                continue
            t = None
            if src == "subtitle":
                raw = pos[i]
                t = max((st for off, st in self.line_starts if off <= raw), default=None)
            return ev.model_copy(update={"source": src, "t": t})
        return None


def normalize_claim(c: Claim, ctx: ExtractContext) -> Claim:
    """名字归一成 id，检查标签和字段，往 flags 里记需要人审的原因。"""
    g = ctx.glossary
    flags = list(c.flags)

    def char_id(name) -> str:
        if not name:
            return ""
        name = str(name).removeprefix("new:")
        t = g.resolve(name, "character")
        if t:
            return t.id
        flags.append(f"未知角色：{name}")
        return str(name)

    def tag_ok(tag) -> bool:
        if not tag:
            return False
        if str(tag) in ctx.kb.tags:
            return True
        name = str(tag).removeprefix("new:")
        if g.resolve(name, "weapon") or g.resolve(name, "artifact"):
            return False  # 模型把武器、圣遗物名当成了功能，这不是新功能，直接忽略
        flags.append(f"词表外功能：{str(tag).removeprefix('new:')}")
        return False

    subject = char_id(c.subject) if c.subject else ctx.core_id
    d = dict(c.data)
    if c.kind == "teammate":
        d["char"] = char_id(d.get("char"))
        d["functions"] = [t for t in d.get("functions") or [] if tag_ok(t)]
    elif c.kind == "slot_function":
        tag_ok(d.get("tag"))
        if d.get("level") not in ("core", "major", "minor"):
            flags.append(f"分级不合法：{d.get('level')}")
    elif c.kind == "team_constraint" and d.get("rule"):
        for tag in re.findall(r"has_tag\(\s*['\"]([^'\"]+)", d["rule"]):
            if tag not in ctx.kb.tags:
                flags.append(f"规则引用了词表外标签：{tag}")
    elif c.kind == "substitution":
        d["original"] = char_id(d.get("original"))
        d["substitute"] = char_id(d.get("substitute"))
    elif c.kind == "cons_effect" and d.get("tag"):
        if not tag_ok(d["tag"]):
            d["tag_note"] = str(d.pop("tag")).removeprefix("new:")
    elif c.kind == "build":
        if d.get("field") in ("sands", "goblet", "circlet", "substat"):
            stat = STAT_IDS.get(_squash_stat(str(d.get("value", ""))))
            if stat:
                d["id"] = stat
        if d.get("field") not in BUILD_FIELDS:
            flags.append(f"养成字段不合法：{d.get('field')}")
        kind = {"weapon": "weapon", "artifact_set": "artifact"}.get(d.get("field"))
        if kind:
            t = g.resolve(str(d.get("value", "")), kind)
            if t:
                d["id"] = t.id
                d["value"] = t.zh
    if c.inferred:
        flags.append("模型推断，视频没直说")
    return c.model_copy(update={"subject": subject, "data": d, "flags": sorted(set(flags))})


def _ask(model: JSONModel, system: str, m: Material, texts, chunk, part, total) -> list[dict]:
    """问一段；输出被截断就把这段对半再问，最多拆到 40 行一段。返回每次回答（可能多个）。"""
    try:
        return [model.complete_json(system, build_user_prompt(m, texts, chunk, (part, total)))]
    except LLMTruncated:
        if len(chunk) <= 40:
            raise
        half = len(chunk) // 2
        return (_ask(model, system, m, texts, chunk[:half], part, total)
                + _ask(model, system, m, texts, chunk[half:], part, total))


def extract_video(m: Material, ctx: ExtractContext, model: JSONModel, chunk_chars: int = 6000) -> VideoClaims:
    texts, lines, counter = corrected_material(m, ctx.glossary)
    tags = "\n".join(f"{t.id}: {t.zh}" for t in ctx.kb.tags.values())
    system = SYSTEM_PROMPT.format(core=ctx.core_zh, tags=tags, chars=ctx.glossary.for_prompt(),
                                  fields=" / ".join(BUILD_FIELDS))
    loc = Locator(texts, lines)
    out = VideoClaims(bvid=m.bvid, title=m.title, up=m.up, published=m.published, model=model.model,
                      corrections={f"{w}→{r}": n for (w, r), n in counter.items() if w != r})
    chunks = chunk_lines(lines, chunk_chars)
    items: list[dict] = []
    for i, chunk in enumerate(chunks, 1):
        answers = _ask(model, system, m, texts, chunk, i, len(chunks))
        if i == 1 and answers[0].get("relevant") is False:
            out.rejected.append({"claim": None, "why": "模型判断不是该角色的原神攻略视频"})
            return out
        for raw in answers:
            items += raw.get("claims") or []
    for item in items:
        try:
            claim = Claim(**{k: v for k, v in item.items() if k in Claim.model_fields and k not in ("bvid", "flags")})
        except (ValidationError, TypeError) as e:
            out.rejected.append({"claim": item, "why": f"格式不对：{str(e).splitlines()[0]}"})
            continue
        found = [e for e in (loc.find(ev) for ev in claim.evidence) if e]
        if not found:
            out.rejected.append({"claim": item, "why": "引用在材料里找不到"})
            continue
        claim = claim.model_copy(update={"bvid": m.bvid,
                                         "evidence": [e.model_copy(update={"bvid": m.bvid}) for e in found]})
        out.claims.append(normalize_claim(claim, ctx))
    return out
