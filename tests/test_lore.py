from app.lore import activate_lore, key_matches


def project(entries):
    return {"world_entries": entries}


def test_primary_secondary_regex_and_negative_filters():
    entries = [
        {
            "id": "qin-law",
            "title": "秦律审讯",
            "keys": ["/秦律|廷尉/i"],
            "secondary_keys": ["审讯", "问供"],
            "selective_logic": "and_any",
            "content": "口供必须复核。",
        },
        {
            "id": "no-war",
            "title": "非战时徭役",
            "keys": ["徭役"],
            "secondary_keys": ["战时"],
            "selective_logic": "not_any",
            "content": "按户籍轮换。",
        },
    ]
    lore = activate_lore(project(entries), "廷尉正在审讯徭役案")
    assert [item["title"] for item in lore] == ["秦律审讯", "非战时徭役"]
    assert key_matches("/qin/i", "QIN law")
    assert not activate_lore(project(entries), "廷尉巡视；战时徭役加倍")


def test_recursive_lore_and_recursion_controls():
    entries = [
        {"id": "a", "title": "客卿", "keys": ["沈砚"], "content": "他受廷尉监视。"},
        {"id": "b", "title": "廷尉", "keys": ["廷尉"], "content": "廷尉遵循复核制。"},
        {"id": "c", "title": "复核制", "keys": ["复核制"], "content": "记录必须二次核验。"},
        {"id": "d", "title": "不可递归", "keys": ["监视"], "content": "不会出现。", "non_recursable": True},
    ]
    lore = activate_lore(project(entries), "沈砚入秦", max_recursion_steps=2)
    assert [item["title"] for item in lore] == ["客卿", "廷尉", "复核制"]
    assert [item["_activation_depth"] for item in lore] == [0, 1, 2]


def test_character_chapter_scope_and_deterministic_inclusion_group():
    entries = [
        {
            "id": "early",
            "title": "前期称谓",
            "keys": ["嬴政"],
            "content": "称秦王政。",
            "order": 5,
            "chapter_end": 10,
            "character_names": ["嬴政"],
        },
        {
            "id": "weak",
            "title": "旧版本",
            "keys": ["咸阳"],
            "content": "旧规则。",
            "inclusion_group": "咸阳制度",
            "order": 10,
        },
        {
            "id": "strong",
            "title": "权威版本",
            "keys": ["咸阳", "廷尉"],
            "match": "all",
            "content": "新规则。",
            "inclusion_group": "咸阳制度",
            "order": 20,
        },
    ]
    lore = activate_lore(
        project(entries),
        "嬴政在咸阳召见廷尉",
        current_chapter_index=4,
        active_character_names={"嬴政"},
    )
    assert [item["title"] for item in lore] == ["前期称谓", "权威版本"]
    late = activate_lore(
        project(entries),
        "嬴政在咸阳召见廷尉",
        current_chapter_index=15,
        active_character_names={"嬴政"},
    )
    assert "前期称谓" not in [item["title"] for item in late]
