from app.memory import relevance, retrieve_memories, terms


def test_terms_support_chinese_bigrams_and_latin():
    result = terms("白塔 Clock_12")
    assert "白塔" in result
    assert "clock_12" in result


def test_retrieve_relevant_and_recent_memories():
    project = {
        "chapters": [
            {"id": "c1", "title": "第一章", "summary": "林墨在白塔丢失了铜钥匙。"},
            {"id": "c2", "title": "第二章", "summary": "苏禾抵达海港调查失踪案。"},
            {"id": "c3", "title": "第三章", "summary": ""},
        ],
        "memory": {
            "facts": [
                {
                    "id": "f1",
                    "text": "铜钥匙已经落入守塔人手中。",
                    "tags": ["钥匙", "白塔"],
                    "importance": 5,
                    "active": True,
                }
            ],
            "plot_threads": [
                {
                    "id": "p1",
                    "title": "钟声之谜",
                    "status": "open",
                    "latest": "钟声会在说谎时响起。",
                }
            ],
            "timeline": [],
        },
    }
    hits = retrieve_memories(project, "林墨回到白塔寻找钥匙", 2, 4)
    contents = "\n".join(hit.content for hit in hits)
    assert "铜钥匙" in contents
    assert any(hit.kind == "thread" for hit in hits)
    assert relevance("白塔钥匙", "他在白塔找到了钥匙") > 0


def test_old_irrelevant_open_threads_do_not_crowd_relevant_memories():
    threads = [
        {
            "id": f"old-{index}",
            "title": f"无关旧线索{index}",
            "status": "open",
            "latest": "发生在遥远海港的支线",
        }
        for index in range(12)
    ]
    project = {
        "chapters": [
            {"id": "c1", "title": "粮仓", "summary": "沈砚发现三套粮册口径冲突。"}
        ],
        "memory": {
            "facts": [
                {
                    "id": "f1",
                    "text": "仓曹原账已被嬴政下令封存。",
                    "tags": ["粮册"],
                    "importance": 5,
                    "active": True,
                }
            ],
            "plot_threads": threads,
            "timeline": [],
        },
    }
    hits = retrieve_memories(project, "沈砚复核粮册", 1, 6)
    assert any("粮册" in hit.content for hit in hits)
    assert not any(hit.source_id == "old-0" for hit in hits)


def test_retrieval_diversifies_near_duplicate_facts():
    project = {
        "chapters": [{"id": "c1", "title": "粮仓", "summary": "沈砚复核粮仓账目。"}],
        "memory": {
            "facts": [
                {"id": f"f{i}", "text": f"粮仓账目显示损耗数字异常{i}", "importance": 5, "active": True, "tags": ["粮仓"]}
                for i in range(8)
            ],
            "plot_threads": [{"id": "p1", "title": "谁改了账", "status": "open", "latest": "仓吏拒绝交出原始木牍"}],
            "timeline": [{"id": "t1", "time": "当夜", "event": "原始木牍被移入封库"}],
        },
    }
    hits = retrieve_memories(project, "沈砚调查粮仓账目和木牍", 1, 6)
    assert sum(hit.kind == "fact" for hit in hits) <= 3
    assert any(hit.kind == "thread" for hit in hits)
