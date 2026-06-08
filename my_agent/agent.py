"""
agent.py
--------
Constructs and compiles the LangGraph agent.

Graph flow:
  START → llm_node ↔ tool_node → verify_node → END
                                      ↓ (RETRY)
                                   llm_node (re-generate SQL)
"""
import asyncio
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from my_agent.utils.nodes import build_tool_node, llm_node, verify_node
from my_agent.utils.state import AgentState
from my_agent.utils.tools import cleanup_tools, init_tools


def should_continue(state: AgentState) -> Literal["tool_node", "verify_node", "__end__"]:
    """
    After llm_node:
    - If the LLM emitted tool calls → run the tools.
    - If verified is True (verify_node already confirmed the answer) → stop.
    - Otherwise → stop (no tool calls and not yet verified means a plain answer).
    """
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        return "tool_node"
    return END


def after_tool_node(state: AgentState) -> Literal["verify_node", "llm_node"]:
    """
    After tool_node executes:
    - If the LLM called execute_sql, go to verify_node to verify the query outcome.
    - If it only called schema retrieval, go back to llm_node so it can generate the SQL using the retrieved context.
    """
    last_ai_message = None
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, AIMessage) or getattr(msg, "__class__", None).__name__ == "AIMessage":
            last_ai_message = msg
            break

    if last_ai_message and getattr(last_ai_message, "tool_calls", None):
        tool_names = [tc["name"] for tc in last_ai_message.tool_calls]
        if "execute_sql" in tool_names:
            return "verify_node"

    return "llm_node"


def after_verify_node(state: AgentState) -> Literal["llm_node", "__end__"]:
    """
    After verify_node:
    - CORRECT (verified=True) → stop; the final AIMessage is already in state.
    - RETRY   (verified=False) → loop back to llm_node to re-generate SQL.
    """
    if state.get("verified", False):
        return END
    return "llm_node"


async def build_graph():
    """
    Initialises MCP tools, builds the graph, compiles, and returns it.
    Call once at application startup.
    """
    await init_tools()
    tool_node = build_tool_node()

    builder = StateGraph(AgentState)
    builder.add_node("llm_node", llm_node)
    builder.add_node("tool_node", tool_node)
    builder.add_node("verify_node", verify_node)

    builder.add_edge(START, "llm_node")

    # llm_node → tool_node (tool call) or END (plain answer)
    builder.add_conditional_edges(
        "llm_node",
        should_continue,
        ["tool_node", END],
    )

    # tool_node goes to verify_node or llm_node
    builder.add_conditional_edges(
        "tool_node",
        after_tool_node,
        ["verify_node", "llm_node"],
    )

    # verify_node → END (correct) or llm_node (retry)
    builder.add_conditional_edges(
        "verify_node",
        after_verify_node,
        ["llm_node", END],
    )

    graph = builder.compile()
    print("Agent graph compiled successfully.")
    return graph


async def main():
    graph = await build_graph()
    user_query = "How many students are in the database?"
    result = await graph.ainvoke(
        {
            "user_query": user_query,
            "messages": [HumanMessage(content=user_query)],
            "retrieved_context": [],
            "llm_calls": 0,
            "verify_calls": 0,
            "verified": False,
        }
    )

    print("\n" + "=" * 60)
    print("CONVERSATION TRACE")
    print("=" * 60)
    for message in result["messages"]:
        message.pretty_print()
    print(f"\nLLM calls made:    {result['llm_calls']}")
    print(f"Verify loops run:  {result['verify_calls']}")
    print(f"Verified:          {result['verified']}")

    await cleanup_tools()


if __name__ == "__main__":
    asyncio.run(main())
