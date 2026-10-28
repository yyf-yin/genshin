"""下载并缓存两份上游数据。

- genshin-db（npm 包，MIT）：角色、天赋、命座、武器、圣遗物，已整理成多语言 JSON。
- AnimeGameData2（GitLab 上的游戏解包数据）：游戏内名词解释、新手教程、天赋参数和文本表。

缓存放在仓库根目录的 .cache/gamedata/，已加入 .gitignore。
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "gamedata"

NPM_REGISTRY = "https://registry.npmjs.org/genshin-db"
AGD_RAW = "https://gitlab.com/Dimbreath/animegamedata2/-/raw/main"
AGD_FILES = {
    "hyperlinks": "ExcelBinOutput/HyperLinkNameExcelConifgData.json",  # 上游文件名就是这么拼的
    "tutorials": "ExcelBinOutput/TutorialDetailExcelConfigData.json",
    "proud_skills": "ExcelBinOutput/ProudSkillExcelConfigData.json",
    "resonance": "ExcelBinOutput/TeamResonanceExcelConfigData.json",
    "element_coeff": "ExcelBinOutput/ElementCoeffExcelConfigData.json",  # 剧变/月曜反应的等级系数
    # 角色技能、天赋、命座的原始配置：genshin-db 没有的「专项调整」（完成任务后生效的新天赋、命座新文本）从这里取
    "avatars": "ExcelBinOutput/AvatarExcelConfigData.json",
    "skill_depots": "ExcelBinOutput/AvatarSkillDepotExcelConfigData.json",
    "skills": "ExcelBinOutput/AvatarSkillExcelConfigData.json",
    "avatar_talents": "ExcelBinOutput/AvatarTalentExcelConfigData.json",
    "textmap": "TextMap/TextMapCHS.json",
    "textmap_medium": "TextMap/TextMap_MediumCHS.json",
}


def _get(url: str, attempts: int = 6) -> bytes:
    """GitLab 对匿名下载限速（429），按指数退避重试。"""
    delay = 5.0
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=600) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == attempts - 1:
                raise
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")


def genshin_db(version: str | None = None, cache_dir: Path = CACHE_DIR, refresh: bool = False) -> tuple[dict, str]:
    """返回 (genshin-db 全量数据, 包版本号)。version 为空时取最新版。"""
    meta = None
    if not version:
        meta = json.loads(_get(NPM_REGISTRY))
        version = meta["dist-tags"]["latest"]
    path = cache_dir / f"genshin-db-{version}.json"
    if refresh or not path.exists():
        meta = meta or json.loads(_get(NPM_REGISTRY))
        tarball = _get(meta["versions"][version]["dist"]["tarball"])
        with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
            raw = tar.extractfile("package/src/min/data.min.json").read()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    with path.open(encoding="utf-8") as f:
        return json.load(f), version


def animegamedata(cache_dir: Path = CACHE_DIR, refresh: bool = False) -> dict[str, object]:
    """返回 {名字: 解析后的 JSON}，名字见 AGD_FILES。"""
    out: dict[str, object] = {}
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name, rel in AGD_FILES.items():
        path = cache_dir / Path(rel).name
        if refresh or not path.exists():
            path.write_bytes(_get(f"{AGD_RAW}/{rel}"))
            time.sleep(2)
        with path.open(encoding="utf-8") as f:
            out[name] = json.load(f)
    return out
