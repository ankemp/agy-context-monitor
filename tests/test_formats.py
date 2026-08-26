import pytest
from context_monitor.formats import BlockParser, BlockType

def test_parse_user_input():
    parser = BlockParser()
    step = {"type": "USER_INPUT", "content": "Hello"}
    blocks = parser.parse_step(step, 1)
    assert len(blocks) == 1
    assert blocks[0].kind == BlockType.USER
    assert blocks[0].text == "Hello"
    assert blocks[0].step_index == 1

def test_parse_planner_response_with_think_field():
    parser = BlockParser()
    step = {
        "type": "PLANNER_RESPONSE",
        "thinking": "I need to do this.",
        "content": "Doing it now.",
        "tool_calls": [{"name": "toolA", "args": {"a": 1}}]
    }
    blocks = parser.parse_step(step, 2)
    assert len(blocks) == 3
    assert blocks[0].kind == BlockType.THINKING
    assert blocks[0].text == "I need to do this."
    
    assert blocks[1].kind == BlockType.ASSISTANT
    assert blocks[1].text == "Doing it now."
    
    assert blocks[2].kind == BlockType.TOOL_USE
    assert blocks[2].name == "toolA"
    assert blocks[2].arguments == {"a": 1}
    assert blocks[2].call_id == "2_0"

def test_parse_planner_response_with_think_tags():
    parser = BlockParser()
    step = {
        "type": "PLANNER_RESPONSE",
        "content": "<think>\nThinking inside tags\n</think>\nActual response"
    }
    blocks = parser.parse_step(step, 3)
    assert len(blocks) == 2
    assert blocks[0].kind == BlockType.THINKING
    assert blocks[0].text == "Thinking inside tags"
    assert blocks[1].kind == BlockType.ASSISTANT
    assert blocks[1].text == "Actual response"

def test_parse_tool_result_implicit_linking():
    parser = BlockParser()
    
    # First the tool call
    step1 = {
        "type": "PLANNER_RESPONSE",
        "content": "Calling tool",
        "tool_calls": [{"name": "toolB", "args": {}}]
    }
    blocks1 = parser.parse_step(step1, 4)
    assert len(blocks1) == 2
    
    # Then the result
    step2 = {
        "type": "GENERIC",
        "content": "Tool output"
    }
    blocks2 = parser.parse_step(step2, 5)
    assert len(blocks2) == 1
    assert blocks2[0].kind == BlockType.TOOL_RESULT
    assert blocks2[0].text == "Tool output"
    assert blocks2[0].call_id == "4_0" # Should pop from pending

def test_parse_system_message():
    parser = BlockParser()
    step = {"type": "SYSTEM_MESSAGE", "content": "System info"}
    blocks = parser.parse_step(step, 6)
    assert len(blocks) == 1
    assert blocks[0].kind == BlockType.SYSTEM
    assert blocks[0].text == "System info"
