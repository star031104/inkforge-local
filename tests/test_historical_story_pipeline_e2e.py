import json

from fastapi.testclient import TestClient

import app.main as main
from app.db import default_project


HISTORICAL_PROSE = "\n\n".join([
    "雨后的咸阳城带着湿土和车辙的腥气。沈砚跟在仓吏身后穿过窄院，檐水沿着瓦当一滴一滴落进石槽。案上铺着三卷木牍，封泥颜色不同，最外一卷的绳结被人重新系过。沈砚没有碰它，只把袖口收紧，先看仓吏把钥匙插进铜锁。",
    "仓吏姓樊，四十上下，手背有常年搬简留下的裂口。他把第一卷推来，语气很硬：‘今日只核这一册。日落之前归还，少一枚木牍，我拿你问罪。’沈砚点头，没有争辩。他先按月份把木牍排开，又把各县送来的口粮数分别记在空白竹片上。",
    "问题很快露出来。蓝田送来的数字与出仓数能对上，杜县却多出七十余石。差额并不大，若放在整季军粮里甚至不起眼，可三处日期恰好都落在同一旬。沈砚把三枚算筹横放在案角，再让樊吏亲手复算一遍，免得对方以为他在数字上做了手脚。",
    "樊吏算到第二遍，脸色才变。他没有承认账目有错，只把竹片压在掌下：‘粮车进城，损耗本就难定。你拿几根算筹，便想说仓中有人偷粮？’沈砚答道：‘我只说三处差额落在同一时段。若是路损，应先查三条路；若是入仓后少了，才查仓门。现在还不能定人。’",
    "院外传来甲士靴底踏水的声音，谈话立刻停住。中郎领着两名卫士进来，没有宣读长令，只让樊吏与沈砚带上木牍，去东侧小厅候问。厅内没有华饰，墙边一盏铜灯，火苗被门风压得偏向一侧。秦王政坐在长案后，先看木牍，再看那三枚被沈砚带来的算筹。",
    "秦王政问：‘你说这账不能定人，那你来见寡人，要定什么？’沈砚把三枚算筹分开摆下：‘定一件事：差额是不是偶然。请把同旬另外两县的入仓木牍也取来。若它们没有相同缺口，我就收回疑问；若也有，便该查这一旬共同经过的人和门。’",
    "樊吏立刻俯身道：‘王上，仓曹旧例不许外人遍阅诸县粮牍。此人来历未明，只凭一处小差便要翻尽旧册，恐乱仓法。’沈砚没有抢话。他知道此刻争的是能不能继续核对，而不是谁在道理上更漂亮。于是他把手从算筹旁收回来，等秦王政自己衡量。",
    "厅中安静了片刻。秦王政没有问沈砚来自何处，也没有替他辩解，只转向樊吏：‘旧例不许外人遍阅，是为防什么？’樊吏答：‘防账目外泄，也防有人借核账改牍。’秦王政又问：‘若只在你面前开册，木牍不离仓曹，封泥由你验，能不能防？’樊吏一时没有答。",
    "沈砚听见灯芯轻响。他把机会压在最小处：‘我不带走一片木牍，也不单独触碰封泥。由樊吏开、樊吏收，我只报出相同日期和数目。若两册之后没有规律，今日到此为止。’这不是胜利，只是一条可以被立即收回的窄路。",
    "秦王政抬手，示意仓吏把封泥未拆的第二册也交给沈砚。樊吏领命时下颌绷得很紧，却仍亲自验了封泥，又把木牍一片片摊在案上。第二册来自雍县，前几日都平稳，到了同一旬末尾，出入之间也出现一处不大的缺口。",
    "沈砚没有抬头去看秦王政。他把那一日的木牍单独移到左侧，又让樊吏复算。樊吏这次算得很慢。窗外最后一线亮光越过门槛时，他把算筹放下，低声报出一个与沈砚相同的数。两册账还不足以指向任何人，却足以证明第一处差额不该被随手抹平。",
    "秦王政起身前只留下一句话：‘第三册明日开。今夜封门，谁也不得补字。’甲士应声。樊吏收卷时看了沈砚一眼，那眼神里没有信任，只有新的戒备。沈砚也没有松气。门一旦封上，明日若第三册干净，今日的怀疑会落回他自己身上；若第三册仍有缺口，仓曹里就有人必须解释同一旬发生了什么。",
])


def _parse_sse(text):
    events=[]
    for block in text.split("\n\n"):
        line=next((x for x in block.splitlines() if x.startswith("data:")),"")
        if line: events.append(json.loads(line[5:].strip()))
    return events


def test_historical_chapter_generate_audit_memory_and_graph(monkeypatch):
    project=default_project("hist-e2e","咸阳旧牍","now")
    project["genre"]="战国历史架空"
    project["book_rules"]="秦统一前称嬴政为秦王政或王上；古人不降智；现代知识只能转译为当时可执行的动作。"
    project["characters"]=[
        {"id":"shen","name":"沈砚","role":"主角","personality":"先核对证据再下判断","knowledge":"知道基础统计思想，不知道秦廷隐秘"},
        {"id":"qin","name":"秦王政","role":"秦王","personality":"重结果，也会追问可执行边界","knowledge":"掌握秦廷公开与机密政务"},
    ]
    project["world_entries"]=[{
        "id":"title-rule","title":"统一前称谓","category":"历史硬事实","keys":["秦王政","嬴政","咸阳"],"secondary_keys":[],
        "content":"公元前221年统一以前，不称皇帝、始皇帝；朝臣场景可称王上，叙述称秦王政。",
        "canon":"hard","position":"after","order":1,"constant":True,"enabled":True,"match":"any","character_names":[],
        "chapter_start":0,"chapter_end":0,"inclusion_group":"","non_recursable":False,"prevent_recursion":False,"delay_until_recursion":False,
    }]
    chapter=project["chapters"][0]
    chapter["title"]="仓门旧牍"
    chapter["plan"].update({"goal":"核对两册粮牍并取得继续查第三册的资格","conflict":"仓吏以旧例阻止扩大核对","turning_point":"第二册同旬也出现缺口","ending_hook":"第三册次日开封"})
    project["settings"]["target_words"]=len(HISTORICAL_PROSE.replace("\n",""))

    async def fake_stream(settings,messages):
        for i in range(0,len(HISTORICAL_PROSE),80):
            yield HISTORICAL_PROSE[i:i+80]
    monkeypatch.setattr(main,"chat_stream",fake_stream)

    async def fake_structured(settings,messages,**kwargs):
        system=messages[0]["content"]
        if "连续性与场景审计员" in system:
            result={"score":96,"verdict":"pass","issues":[],"strengths":["称谓与行动逻辑一致"],"revision_brief":"无需修改"}
        elif "状态观察员" in system:
            result={
                "summary":"沈砚用两册粮牍证明同一旬差额并非孤例，秦王政准许次日开第三册。",
                "story_so_far":"沈砚在咸阳仓曹取得有限核账资格，第二册出现同旬缺口，调查进入第三册。",
                "character_updates":[{"name":"沈砚","state":"取得有限核账资格","location":"咸阳仓曹","evidence":"秦王政抬手，示意仓吏把封泥未拆的第二册也交给沈砚。"}],
                "facts":[{"text":"秦王政准许沈砚核对第二册粮牍","importance":4,"confidence":"confirmed","evidence":"秦王政抬手，示意仓吏把封泥未拆的第二册也交给沈砚。"}],
                "plot_threads":[{"title":"同旬粮差来源","status":"open","latest":"第二册也出现同旬缺口","evidence":"第二册来自雍县，前几日都平稳，到了同一旬末尾，出入之间也出现一处不大的缺口。"}],
                "timeline":[{"time":"当日傍晚","event":"第二册核账出现同旬缺口","location":"咸阳仓曹","participants":["沈砚","樊吏","秦王政"],"evidence":"第二册来自雍县"}],
                "relationship_updates":[{"left":"沈砚","right":"秦王政","state":"谨慎试用","tension":"仍未信任","evidence":"第三册明日开。今夜封门，谁也不得补字。"}],
                "continuity_notes":[],"description_updates":[],"scene_settlement":{},
            }
        else:
            raise AssertionError(system[:80])
        if kwargs.get("validate"): kwargs["validate"](result)
        return result,[]
    monkeypatch.setattr(main,"structured_completion",fake_structured)

    client=TestClient(main.app)
    preview=client.post("/api/prompt/preview",json={"project":project,"chapter_id":chapter["id"],"mode":"instruction","instruction":"写核账场景","selection":"","target_words":project["settings"]["target_words"]})
    assert preview.status_code==200
    assert any(item.get("title")=="统一前称谓" for item in preview.json()["activated_lore"])

    generated=client.post("/api/generate",json={"project":project,"chapter_id":chapter["id"],"mode":"instruction","instruction":"严格执行本章计划","selection":"","target_words":project["settings"]["target_words"]})
    events=_parse_sse(generated.text)
    draft="".join(e.get("text","") for e in events if e.get("type")=="token")
    assert "皇帝" not in draft and "秦王政" in draft
    assert len(draft.replace("\n","")) >= 900

    audit=client.post("/api/chapter/audit",json={"project":project,"chapter_id":chapter["id"],"draft":draft,"instruction":""})
    assert audit.status_code==200
    assert audit.json()["verdict"]=="pass",audit.json()

    project["chapters"][0]["content"]=draft
    memory=client.post("/api/chapter/memory",json={"project":project,"chapter_id":chapter["id"],"draft":draft,"instruction":""})
    assert memory.status_code==200
    applied=client.post("/api/chapter/memory/apply",json={"project":project,"chapter_id":chapter["id"],"result":memory.json()})
    assert applied.status_code==200
    updated=applied.json()["project"]
    assert any("第二册" in fact.get("text","") for fact in updated["memory"]["facts"])
    graph=client.post("/api/knowledge/graph",json={"project":updated})
    assert graph.status_code==200
    assert any(node.get("name")=="沈砚" for node in graph.json()["nodes"])

