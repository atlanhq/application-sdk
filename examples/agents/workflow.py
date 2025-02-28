# type: ignore
import json
from typing import Annotated, List, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.graph.message import AnyMessage
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel

from application_sdk.agents import get_llm

load_dotenv()
llm = get_llm()
state = {
    "messages": [],
    "enriched_query": "",
    "steps_list": [],
    "validation_response": "",
}


class AtlanAgentTaskStep(TypedDict):
    """
    AtlanAgentTaskStep is a model for a task step in the AtlanAgent.
    """

    step_id: int
    description: str
    tool: str
    completed: bool


class AtlanAgentTaskSteps(BaseModel):
    """
    TaskSteps is a list of TaskStep objects for structured output from the planner agent.
    """

    steps: List[AtlanAgentTaskStep]
    validation_response: str

    def to_dict(self):
        return {"steps": self.steps, "validation_response": self.validation_response}


class AtlanAgentState(TypedDict):
    user_query: str
    messages: Annotated[list[AnyMessage], add_messages]
    steps_list: list[AtlanAgentTaskStep]
    validation_response: str
    enriched_query: str


@tool
def search_comics_tool():
    """
    Search for comics in the library
    """
    return {"results": ["Comic 1", "Comic 2", "Comic 3"]}


@tool
def search_horror_tool():
    """
    Search for horror in the library
    """
    return {"results": ["Horror 1", "Horror 2", "Horror 3"]}


@tool
def search_encyclopedia_tool():
    """
    Search for encyclopedia in the library
    """
    return {"results": ["Encyclopedia 1", "Encyclopedia 2", "Encyclopedia 3"]}


enrichment_system_prompt = """
You are a helpful assistant that enriches the user query, in terms of library search, and the enriched query should contain the expected results
"""

enrichment_user_prompt = """
{user_query}
"""

planner_system_prompt = """
You are a helpful assistant that plans the steps for the library search, you have the following tools available:
{tools}
"""

planner_user_prompt = """
{query}
"""


def enrichment_node(state: AtlanAgentState):
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", enrichment_system_prompt),
            (
                "human",
                enrichment_user_prompt.format(
                    user_query=state["messages"][-1].content,
                ),
            ),
        ]
    ).format_messages()
    response = llm.invoke(prompt)
    ai_message = AIMessage(content=response.content)
    return {
        "messages": [ai_message],
        "enriched_query": response.content,
    }


def planner_agent(state: AtlanAgentState):
    tools = [search_comics_tool, search_horror_tool, search_encyclopedia_tool]
    structured_llm = llm.with_structured_output(AtlanAgentTaskSteps)
    prompt = ChatPromptTemplate.from_messages(
        [
            # passing the tools to the planner agent to know which tools are available to the atlan_search_agent
            (
                "system",
                planner_system_prompt.format(tools=tools),
            ),
            (
                "human",
                planner_user_prompt.format(query=state["enriched_query"]),
            ),
        ]
    ).format_messages()
    response = structured_llm.invoke(prompt)
    # Convert the steps to AIMessage format to ensure proper structure
    # Convert steps to string to avoid type issues
    ai_message = AIMessage(content=json.dumps(response.steps))
    human_message = HumanMessage(content=state["messages"][-1].content)
    return {
        "messages": [ai_message, human_message],
        "steps_list": response.steps,
        "validation_response": response.validation_response,
    }  # this will update the state


def agent_node(state: AtlanAgentState):
    messages = state["messages"]
    llm_with_tools = llm.bind_tools(tools)
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


tools = [search_comics_tool, search_horror_tool, search_encyclopedia_tool]
tool_node = ToolNode(tools=tools)


def get_state_graph():
    workflow = StateGraph(AtlanAgentState)
    workflow.add_node("enrichment_node", enrichment_node)
    workflow.add_node("planner_agent", planner_agent)
    workflow.add_node("tool_node", tool_node)
    workflow.add_node("agent_node", agent_node)
    workflow.add_edge(START, "enrichment_node")
    workflow.add_edge("enrichment_node", "planner_agent")
    workflow.add_edge("planner_agent", "agent_node")
    workflow.add_conditional_edges(
        "agent_node", tools_condition, {"tools": "tool_node", "__end__": END}
    )
    workflow.add_edge("tool_node", "agent_node")
    workflow.add_edge("agent_node", END)
    return workflow
