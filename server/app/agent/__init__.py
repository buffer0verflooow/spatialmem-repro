"""物品识别 Agent 模块。

基于 Qwen-VL function calling 实现的智能物品识别 Agent，
支持 HTTP API、交互式对话和 CLI 三种使用方式。
"""

from app.agent.runner import AgentRunner, AgentSession
from app.agent.tools import TOOLS, execute_tool

__all__ = ["AgentRunner", "AgentSession", "TOOLS", "execute_tool"]
