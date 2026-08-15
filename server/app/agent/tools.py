"""Agent 工具定义。

使用 OpenAI function calling 格式定义工具，供 Qwen-VL 模型调用。
"""

from __future__ import annotations

import json
from typing import Any

from app.kb.store import KbStore
from app.observability import get_logger

log = get_logger(__name__)


# OpenAI function calling 格式的工具定义
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "recognize_objects",
            "description": "识别图像中的所有物品，返回名称、位置、数量、详细描述。当用户要求识别物品、列出看到的东西时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "detail_level": {
                        "type": "string",
                        "enum": ["brief", "detailed"],
                        "description": "识别详细程度：brief 只返回名称和位置，detailed 包含完整描述",
                        "default": "detailed",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "用关键词在知识库中检索物品的详细信息、使用方法和注意事项。当需要获取某个物品的专业知识时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，可以是物品名称或描述",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_scene",
            "description": "描述整体场景，包括环境类型、光线条件、空间布局、潜在风险。当需要了解整体环境而非具体物品时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


async def execute_tool(
    name: str,
    args: dict[str, Any],
    image_b64: str | None,
    kb: KbStore,
) -> str:
    """执行工具调用，返回 JSON 字符串结果。

    Args:
        name: 工具名称
        args: 工具参数
        image_b64: 图片 base64（recognize_objects 和 describe_scene 需要）
        kb: 知识库实例（search_knowledge 需要）

    Returns:
        工具执行结果的 JSON 字符串
    """
    log.info("agent_tool_call", tool=name, args=args)

    if name == "recognize_objects":
        return await _recognize_objects(args, image_b64)
    elif name == "search_knowledge":
        return await _search_knowledge(args, kb)
    elif name == "describe_scene":
        return await _describe_scene(args, image_b64)
    else:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


async def _recognize_objects(args: dict[str, Any], image_b64: str | None) -> str:
    """物品识别工具。

    注意：这个工具本身不直接调用模型，而是返回一个标记让 Agent runner
    在下一轮对话中使用专门的识别提示词。实际识别在 runner 层完成。
    """
    detail_level = args.get("detail_level", "detailed")

    if not image_b64:
        return json.dumps(
            {"error": "没有提供图片，无法进行物品识别"},
            ensure_ascii=False,
        )

    # 返回结构化指令，让模型用专门的提示词进行识别
    return json.dumps(
        {
            "status": "ready",
            "detail_level": detail_level,
            "instruction": (
                "请仔细分析图片，识别所有可见物品。"
                + ("对每个物品提供详细描述。" if detail_level == "detailed" else "只列出名称和位置。")
            ),
        },
        ensure_ascii=False,
    )


async def _search_knowledge(args: dict[str, Any], kb: KbStore) -> str:
    """知识库检索工具。"""
    query = args.get("query", "")
    top_k = args.get("top_k", 3)

    if not query.strip():
        return json.dumps({"error": "搜索关键词不能为空"}, ensure_ascii=False)

    if not kb.ready:
        return json.dumps(
            {"status": "unavailable", "message": "知识库未加载，无法进行检索"},
            ensure_ascii=False,
        )

    try:
        results = await kb.search(query, top_k, 0.5)
        return json.dumps(
            {
                "status": "ok",
                "query": query,
                "results": results,
                "count": len(results),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        log.warning("agent_tool_kb_search_failed", error=str(exc))
        return json.dumps(
            {"status": "error", "message": f"检索失败: {exc}"},
            ensure_ascii=False,
        )


async def _describe_scene(args: dict[str, Any], image_b64: str | None) -> str:
    """场景描述工具。与 recognize_objects 类似，返回指令让模型进行场景分析。"""
    if not image_b64:
        return json.dumps(
            {"error": "没有提供图片，无法进行场景描述"},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "status": "ready",
            "instruction": "请描述整体场景：环境类型、空间布局、光线条件、氛围、潜在风险或注意事项。",
        },
        ensure_ascii=False,
    )
