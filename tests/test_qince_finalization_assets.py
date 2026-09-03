import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


def _memory_key(value: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]", "", str(value)).casefold()


def test_all_qince_memory_assets_are_evidence_backed_and_have_no_workflow_warning():
    for number in range(1, 101):
        memory = json.loads(
            (ARTIFACTS / f"qince-chapter-{number:03d}-memory.json").read_text(encoding="utf-8")
        )
        prose = _memory_key(
            (ARTIFACTS / f"qince-chapter-{number:03d}-editorial.md").read_text(encoding="utf-8")
        )
        assert "warnings" not in memory
        for group in (
            "character_updates",
            "facts",
            "plot_threads",
            "timeline",
            "relationship_updates",
        ):
            for item in memory.get(group, []):
                evidence = _memory_key(item.get("evidence", ""))
                assert evidence, (number, group, item)
                assert evidence in prose, (number, group, item.get("title") or item.get("name") or item.get("text"))


def test_qince_final_memory_closes_every_named_long_term_debt():
    memory = json.loads(
        (ARTIFACTS / "qince-chapter-100-memory.json").read_text(encoding="utf-8")
    )
    by_title = {item["title"]: item for item in memory["plot_threads"]}
    expected = {
        "终局制度边界",
        "全国名籍与空白户牒",
        "杜衡死亡责任",
        "咸阳之眼",
        "四木牍争道",
        "南岸盲区旧债",
        "樊氏藏役案",
        "王绾毁牒问责",
        "焚牒误传",
        "名籍之外的人",
    }
    assert expected <= by_title.keys()
    for title in expected:
        assert by_title[title]["status"] in {
            "resolved",
            "resolved_with_boundary",
            "resolved_as_process",
        }
        assert by_title[title].get("payoff")


def test_chapter_89_memory_does_not_leak_the_chapter_100_annual_service_decision():
    memory = json.loads(
        (ARTIFACTS / "qince-chapter-089-memory.json").read_text(encoding="utf-8")
    )
    facts = "\n".join(item.get("text", "") for item in memory.get("facts", []))
    assert "接受逐年核役" not in facts


def test_secure_finalizer_accepts_no_api_key_argument_or_literal_secret():
    source = (ROOT / "scripts" / "finalize-qince-siliconflow.py").read_text(encoding="utf-8")
    assert "--api-key" not in source
    assert "sk-" not in source
    assert '"api_key": ""' in source


def test_secure_server_launcher_is_ascii_for_windows_powershell_compatibility():
    source = (ROOT / "scripts" / "run-secure-server.ps1").read_text(encoding="utf-8")
    assert source.isascii()
    assert "Read-Host" in source and "-AsSecureString" in source
