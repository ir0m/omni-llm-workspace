from omniws.agents.handover import describe_part
from omniws.history import chat_history, join_text
from omniws.models import Message


def test_join_text_keeps_paragraphs():
    assert join_text(["調べます。", "## 結果"]) == "調べます。\n\n## 結果"
    assert join_text(["a\n", "b"]) == "a\n\nb"
    assert join_text(["a\n\n", "b"]) == "a\n\nb"
    assert join_text(["a", "\n\nb"]) == "a\n\nb"
    assert join_text(["a\n\n\n", "b"]) == "a\n\n\nb"  # never removes what the model wrote
    assert join_text(["", "a", "", "b"]) == "a\n\nb"


def blocks():
    return [{"type": "text", "text": "Web検索します。"}, {"type": "tool_call", "id": "t", "title": "Web検索"},
            {"type": "text", "text": "## 結果\n- 晴れ"}]


def test_history_and_handover_keep_headings_after_tool_calls():
    messages = [Message(id="u", seq=1, role="user", status="complete", content=[{"type": "text", "text": "天気"}]),
                Message(id="a", seq=2, role="assistant", status="complete", content=blocks())]
    assert chat_history(messages)[1]["content"] == "Web検索します。\n\n## 結果\n- 晴れ"
    assert "Web検索します。\n\n## 結果" in describe_part(messages[1], "assistant", blocks())
