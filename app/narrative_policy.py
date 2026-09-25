"""Genre-aware guidance, separate from immutable story facts."""
from typing import Any

PROFILES = {
    "adaptive": ("因果与人物", "根据人物欲望、上一场后果和未解决冲突选择下一步。允许行动、观察、余波或关系积累；不按章序机械轮换。"),
    "mystery": ("悬疑与揭示", "跟踪读者已见线索、合理误判与待解问题。揭示应回指证据；不要求每章破案或制造反转。"),
    "relationship": ("关系与情感", "关注距离、信任、误解和边界的细微变化；允许日常共处和沉默积累意义，不强制每场争执或损失。"),
    "adventure": ("行动与探索", "关注空间、目标、能力限制、资源和行动后果；紧张场面与余波交替，由具体情势决定节奏。"),
    "literary": ("观察与人物", "允许感知、日常、内在矛盾和意象承担推进；保留含混与留白，不强求悬崖结尾和显性代价。"),
    "custom": ("作者自定义", "遵循作者自定义结构；若未填写，则按人物选择和场景后果自然推进。"),
    "legacy": ("经典十二岗位", "使用固定十二岗位轮换；适合需要严格分卷节拍的项目。"),
}


def profile(project: dict[str, Any]) -> str:
    value = project.get("narrative", {}).get("structure_profile", "adaptive")
    return value if value in PROFILES else "adaptive"


def guidance(project: dict[str, Any]) -> str:
    key = profile(project)
    label, method = PROFILES[key]
    custom = str(project.get("narrative", {}).get("custom_structure", ""))[:3000]
    return f"叙事策略：{label}。{method}\n{custom if key == 'custom' else ''}\n结构方法服从作者要求和既定事实，不是所有章节都必须套用的检查公式。"


def route_guidance(project: dict[str, Any], final: bool) -> tuple[str, str, str]:
    return (
        guidance(project) + ("这是卷末，应兑现本卷承诺并保留余韵。" if final else "承接上一章的具体后果，选择本章最有价值的场景功能。"),
        "由本章人物行动决定的信息、关系、体验或处境变化；允许多个相关维度",
        "不得重复已经完成的事件，不得违背事实或提前兑现明确保留的后续揭示。",
    )
