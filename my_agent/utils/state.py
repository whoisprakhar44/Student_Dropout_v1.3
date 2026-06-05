import operator
from typing import List, Dict, Any
from typing_extensions import Annotated, NotRequired, TypedDict
from langchain_core.messages import AnyMessage


class AgentState(TypedDict):
    # Original user query
    user_query: str

    # Context chunks returned by RAG MCP server
    retrieved_context: List[Dict[str, Any]]

    # Full conversation / tool-call message history
    messages: Annotated[List[AnyMessage], operator.add]

    # Tracks how many times the LLM has been invoked
    llm_calls: int

    # Set when a template SQL fast-path is used (skip extra LLM turns)
    fast_sql: NotRequired[str | None]

    # Tracks how many verification rounds have run (max 5)
    verify_calls: int

    # True once the LLM confirms the SQL result is correct
    verified: bool
