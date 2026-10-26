"""游戏本体数据（角色、武器、圣遗物、机制说明），由导入脚本从上游生成到 data/game/。"""

from .store import GameData, load_gamedata

__all__ = ["GameData", "load_gamedata"]
