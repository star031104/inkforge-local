"""Normalize reviewed Qince memory assets and verify evidence against prose."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"

EVIDENCE_FIXES = {
    (8, "character", "秦策"): "九十七枚算筹仍全部压在主营求粮牒旁。选择没有改变，最后一笔却还没有落下。",
    (8, "timeline", "秦策决定九十七辆车全部救主营，弃援令起草但尚未署名。"): "九十七枚算筹仍全部压在主营求粮牒旁。选择没有改变，最后一笔却还没有落下。",
    (20, "relationship", "王绾—秦策"): "“可以解释少数，”他说，“不能据此断定每一个少数都已离境。”",
    (24, "character", "王绾"): "至于如实报告本身，没有一条单独减责，也没有一条保证上级先补粮再问责。",
    (34, "relationship", "秦策—嬴政"): "决策栏下有嬴政的姓名与王印。",
    (46, "relationship", "王绾—嬴政"): "看见得越快，越不能只留成事后的定本。",
    (51, "relationship", "嬴政—秦策"): "西市桥旁多了二十三户挑夫，而是否切断粮路的决定，仍不在秦策手中。",
    (62, "character", "吕陂"): "“人和骡能过。”他说，“秦军的车过不了。”",
    (64, "character", "王绾"): "“这是借你的网救人，”王绾说，“不是把关口还给你。”",
    (68, "thread", "井陉假粮路"): "“先让人活过今天。”他说，“明日逐项署名说明。”",
    (70, "thread", "井陉假粮路"): "赵地一日牍因此获得正式效力。",
    (71, "relationship", "嬴政—秦策"): "是否采用哪一尺、如何处理旧债与车轨，由咸阳收齐勘校牍后裁断。",
    (72, "thread", "四木牍争道"): "当地执法吏要把宗庙祭粮与新尺、新量相校，长老拒绝搬出祖传青铜量器。",
    (74, "thread", "四木牍争道"): "统一车轨令要求军道按新轨重修，楚地工匠却把祖传铜范与旧轮具摆在断路中央，集体停手。",
    (77, "thread", "四木牍争道"): "他没有先去孙禾或米商的铺子，而是走向城西李氏寡妇的门。",
    (81, "character", "嬴政"): "“不是豁免。”嬴政说，“是十日。”",
    (83, "thread", "全国名籍与空白户牒"): "“亡籍、迁籍，还是尚未补籍？”",
    (87, "fact", "药铺近两月多次向同一城外窑场送药，账角均有季禾式木片拓痕；最近三次与病童病程相合。"): "最近三次送药的日期，与季禾所说病童发热、药工学徒两次送煎的时刻恰好相接。",
    (87, "thread", "焚牒误传"): "县署后院有秦策、杜生和见证牍；村里听到的却只有“烧掉一片，少服一役”。",
    (89, "fact", "全国名籍定于下月朔日正式启用，空白牒依十日回报主动追查；季禾第一行待议，病童户牒仍空白。"): "全国名籍定于下月朔日启用。届时各县已核姓名并入新册，空白户牒依十日回报主动追查；未决个案另列待议，不得以待议为由暂停其他地方名籍。",
    (89, "relationship", "王绾—秦策"): "杜生同意封存，仍注明这不是不登记病童的最终裁定。",
    (91, "thread", "全国名籍与空白户牒"): "全国名籍的追查牍仍从东门发出，各县也没有因这一场朝议少查一张空白。",
    (91, "relationship", "秦策—无名病童"): "嬴政命人把病童空白户牒留在御案三日。",
    (93, "thread", "终局制度边界"): "“那就先说明停令须有何门槛。”嬴政说，“不是把印交回县吏手里。”",
    (94, "relationship", "秦策—嬴政"): "停令由此不再只是秦策临机翻红木牌、盖三日印的私意，却仍没有成为县吏手中的权力。",
    (95, "fact", "每一级须记录所收命令、相冲证据、上报改令与实际行动；奉令不自动免责，最下层也不自动承担全部。"): "每一级须分别记四件事：收到什么命令，执行前看见什么相冲证据，是否上报或请求改令，最终做了什么。",
    (95, "thread", "终局制度边界"): "救人者的姓名，已经排在新条文最先可能伤到的位置。",
    (95, "relationship", "秦策—王绾"): "他不把王绾的沉默当作南岸盲区免责理由，也不把县书吏提前传令当作田鹤误类的全部责任。",
    (96, "thread", "终局制度边界"): "他把灰中那半枚官署封泥从证物袋中取出，隔着布看了一会儿，又放回自己案前。",
    (97, "fact", "杜母证词目前只在杜衡验尸附件，因现行身份规则无法进入责任官卷；她仍在御史府门外。"): "她能证明儿子是谁，却没有一个栏位让她以受害者家属身份陈述儿子为何死。",
    (97, "fact", "第二稿受害者陈述条款仍未生效，正式全国申诉入口尚不存在。"): "门内，第二稿刚写下“受害者陈述不得删除”。门外，真正带着陈述来的人仍没有进入那一栏的身份。",
    (98, "thread", "终局制度边界"): "廷尉允许他带走一份已经登记的草案副本和空白木片，在狱中于次日朝议前写出最后意见；原卷、杜母陈述和小印都留在官署，不能由他带走或毁改。",
    (98, "relationship", "王绾—秦策"): "两人选择的保护方式仍不同；只是这一次，门外的人不靠任何一人的秘密保留，已经在官卷中留下了可追的页号。",
    (99, "fact", "第二条规定起草、核数、批准、转递、执行、改令或拒改、事后查验七处逐级署名，按当时可知可为定责，奉令与下令均不自动免责。"): "每道命令须保留起草、核数、批准、转递、执行、改令或拒改、事后查验七处姓名与时刻。",
    (100, "thread", "咸阳之眼"): "也不能再只带着无主命令离开咸阳。",
    (100, "relationship", "秦策—嬴政"): "限时止执、逐级署名、受害者陈述留卷，从这一刻生效。全国名籍与追查也继续生效。",
}

NON_CHARACTER_RELATION_ENDPOINTS = {
    "北岸病棚", "河东郡守", "咸阳受理处", "临河县令", "丞相府",
    "芦县", "邻郡粮营", "地方官吏", "秦国军营", "近郊村户",
    "国家名籍", "无名者", "东乡县吏", "廷尉", "御史府", "制度旧案",
}


def compact(text: str) -> str:
    return re.sub(
        r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]", "", str(text or "")
    ).casefold()


def item_key(kind: str, item: dict) -> str:
    if kind == "character":
        return str(item.get("name", ""))
    if kind == "fact":
        return str(item.get("text", ""))
    if kind == "thread":
        return str(item.get("title", ""))
    if kind == "timeline":
        return str(item.get("event", ""))
    if kind == "relationship":
        left = str(item.get("left") or item.get("from") or "")
        right = str(item.get("right") or item.get("to") or "")
        return f"{left}—{right}"
    return ""


def main() -> None:
    unresolved: list[str] = []
    for number in range(1, 101):
        memory_path = ARTIFACTS / f"qince-chapter-{number:03d}-memory.json"
        prose_path = ARTIFACTS / f"qince-chapter-{number:03d}-editorial.md"
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
        prose = prose_path.read_text(encoding="utf-8")
        memory.pop("warnings", None)
        memory["relationship_updates"] = [
            item
            for item in memory.get("relationship_updates", [])
            if str(item.get("left") or item.get("from") or "")
            not in NON_CHARACTER_RELATION_ENDPOINTS
            and str(item.get("right") or item.get("to") or "")
            not in NON_CHARACTER_RELATION_ENDPOINTS
        ]

        # This claim becomes true only in chapter 100; keeping it in chapter 89
        # would teach the long-term memory a future fact too early.
        if number == 89:
            memory["facts"] = [
                item for item in memory.get("facts", [])
                if not str(item.get("text", "")).startswith("季禾报出自身姓名旧籍经历并接受逐年核役")
            ]

        groups = (
            ("character", memory.get("character_updates", [])),
            ("fact", memory.get("facts", [])),
            ("thread", memory.get("plot_threads", [])),
            ("timeline", memory.get("timeline", [])),
            ("relationship", memory.get("relationship_updates", [])),
        )
        for kind, items in groups:
            for item in items:
                key = item_key(kind, item)
                replacement = EVIDENCE_FIXES.get((number, kind, key))
                if replacement:
                    item["evidence"] = replacement

        if number == 70:
            for item in memory.get("plot_threads", []):
                if item.get("title") == "井陉假粮路":
                    item["payoff"] = "赵地一日牍获得正式效力，救急经验被限定为不自动续权、须重新满足条件并署名的一般规则。"

        if number == 100:
            for item in memory.get("plot_threads", []):
                if item.get("title") == "终局制度边界":
                    item["payoff"] = "限时止执、逐级署名与受害者陈述留卷三条正式生效，并首先追责其起草者秦策。"
                elif item.get("title") == "全国名籍与空白户牒":
                    item["payoff"] = "全国名籍继续运行，病童以待属缓录进入可见、可领药粮但未被强归户的状态。"
                elif item.get("title") == "杜衡死亡责任":
                    item["payoff"] = "杜母陈述成为官卷中不可删除的一页，四个责任断点依逐级署名继续裁定。"
            additions = [
                    {"title":"名籍之外的人","status":"resolved_with_boundary","latest":"病童以待属缓录进入名籍，可领取药粮而不被强归季氏或散失原户；姓名栏仍空，并须每十日复核。","payoff":"领粮资格、被国家看见后的保护与被错误归户的风险同时获得制度化边界；名籍承认其为真实的人，却不替她写完身份。","expected_payoff":"已完成。","payoff_condition":"无。","target_window":"resolved","stakeholders":["秦策","王绾","季禾","嬴政","无名病童"],"knowledge_holders":["公开官卷与名籍官署"],"evidence":"她不再因无名而拿不到药，也没有被一个方便的姓名永久决定属于谁。"},
                    {"title":"四木牍争道","status":"resolved","latest":"统一尺度、道路和粮税保留，旧制实际伤害进入限时止执、逐级署名和受害者陈述规则。","payoff":"没有退回各国旧尺，也没有让统一尺度成为唯一现实；终局三条为地方实害留下正式纠错边界。","expected_payoff":"已完成。","payoff_condition":"无。","target_window":"resolved","stakeholders":["秦策","嬴政","王绾","各地受影响者"],"knowledge_holders":["公开官卷与朝堂"],"evidence":"嬴政没有接受烧毁名籍或退回各国旧尺的建议。"},
                    {"title":"南岸盲区旧债","status":"resolved","latest":"秦策的南岸主动留白被正式列入问证和三年刑责，季禾则以本人姓名登记、旧役逐年另核。","payoff":"空白救人和被利用的双重后果均留卷，未被追认合法，也未恢复重复副牒。","expected_payoff":"已完成。","payoff_condition":"无。","target_window":"resolved","stakeholders":["秦策","王绾","季禾"],"knowledge_holders":["廷尉与公开官卷"],"evidence":"南岸留白由秦策主动决定。"},
                    {"title":"樊氏藏役案","status":"resolved","latest":"樊氏两名藏工进入逐人问证，不以全体补登或销毁证据草率结案。","payoff":"豪强利用名籍空隙的案件进入按人、按证据追责的正式程序。","expected_payoff":"已完成制度性回收。","payoff_condition":"个别刑责依法续办。","target_window":"resolved","stakeholders":["樊氏管事","两名藏工","县吏"],"knowledge_holders":["廷尉与名籍官署"],"evidence":"樊氏两名藏工进入逐人问证。"},
                    {"title":"王绾毁牒问责","status":"resolved","latest":"王绾被免去名籍复核职，灰证交廷尉另案定责，其反对意见仍留附卷。","payoff":"秘密毁牒不获赦免，但原牒、灰证、主动交代与制度反证均保留并分别裁定。","expected_payoff":"已完成制度性回收。","payoff_condition":"个别刑责依法续办。","target_window":"resolved","stakeholders":["王绾","秦策","廷尉"],"knowledge_holders":["廷尉与公开官卷"],"evidence":"王绾的毁牒责任没有被赦免。"},
                    {"title":"焚牒误传","status":"resolved","latest":"十一户焚牒造成的逃散、扣押和证据损失未被王绾救人动机抹去，并进入终局责任卷。","payoff":"公开规则区分原牒、重复件、证据封存与经手责任，焚牒不再能被简化为免役办法。","expected_payoff":"已完成。","payoff_condition":"无。","target_window":"resolved","stakeholders":["王绾","卢翁","近郊十一户","秦策"],"knowledge_holders":["公开官卷与名籍官署"],"evidence":"受害者陈述保留了李氏、田葵、杜母与季禾的话，也保留韩家逃走、樊氏藏役和十一户焚牒的后果。"},
                ]
            existing_titles = {item.get("title") for item in memory.get("plot_threads", [])}
            memory.setdefault("plot_threads", []).extend(
                item for item in additions if item["title"] not in existing_titles
            )

        prose_compact = compact(prose)
        verification_groups = (
            ("character", memory.get("character_updates", [])),
            ("fact", memory.get("facts", [])),
            ("thread", memory.get("plot_threads", [])),
            ("timeline", memory.get("timeline", [])),
            ("relationship", memory.get("relationship_updates", [])),
        )
        for kind, items in verification_groups:
            for item in items:
                evidence = compact(item.get("evidence", ""))
                if evidence and evidence not in prose_compact:
                    unresolved.append(f"第{number}章 {kind} {item_key(kind, item)}")

        memory_path.write_text(
            json.dumps(memory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if unresolved:
        raise SystemExit("仍有未落在正文中的证据：\n" + "\n".join(unresolved))
    print("memory assets normalized; all evidence verified")


if __name__ == "__main__":
    main()
