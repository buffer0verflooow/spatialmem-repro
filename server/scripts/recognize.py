#!/usr/bin/env python3
"""物品识别 CLI 脚本。

支持单次识别和交互式对话两种模式。

用法：
    # 单次识别（需要配置 DASHSCOPE_API_KEY）
    python scripts/recognize.py --image photo.jpg

    # 简要模式
    python scripts/recognize.py --image photo.jpg --brief

    # 带附加问题
    python scripts/recognize.py --image photo.jpg --question "这是什么品牌的手机？"

    # 交互式对话模式
    python scripts/recognize.py --image photo.jpg --chat

    # 使用不同的 API endpoint
    python scripts/recognize.py --image photo.jpg --base-url https://dashscope.aliyuncs.com/compatible-mode/v1
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_image(path: str) -> bytes:
    """读取图片文件。"""
    p = Path(path)
    if not p.exists():
        print(f"错误：文件不存在 {path}", file=sys.stderr)
        sys.exit(1)

    # 检查文件大小
    size = p.stat().st_size
    if size > 20 * 1024 * 1024:
        print(f"错误：文件过大 ({size / 1024 / 1024:.1f}MB > 20MB)", file=sys.stderr)
        sys.exit(1)

    return p.read_bytes()


def print_separator(char: str = "-", width: int = 60) -> None:
    print(char * width)


def print_result(text: str, objects: list | None = None, latency_ms: int = 0) -> None:
    """格式化打印识别结果。"""
    print_separator("=")
    print("识别结果：")
    print_separator("-")
    print(text)

    if objects:
        print_separator("-")
        print(f"\n检测到 {len(objects)} 个物品：")
        for i, obj in enumerate(objects, 1):
            name = obj.get("name", "未知")
            name_en = obj.get("name_en", "")
            print(f"  {i}. {name} ({name_en})")

    if latency_ms:
        print_separator("-")
        print(f"耗时：{latency_ms}ms")
    print_separator("=")


async def run_recognize(args: argparse.Namespace) -> None:
    """单次识别模式。"""
    from app.agent.runner import AgentRunner
    from app.config import Settings

    # 构建配置
    settings_kwargs = {}
    if args.api_key:
        settings_kwargs["dashscope_api_key"] = args.api_key
    if args.base_url:
        settings_kwargs["dashscope_base_url"] = args.base_url
    if args.model:
        settings_kwargs["agent_model"] = args.model

    # 如果没有 API key，尝试从环境变量获取
    api_key = args.api_key or os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("错误：需要提供 DASHSCOPE_API_KEY", file=sys.stderr)
        print("  --api-key sk-xxx 或 export DASHSCOPE_API_KEY=sk-xxx", file=sys.stderr)
        sys.exit(1)

    settings_kwargs["dashscope_api_key"] = api_key
    settings_kwargs["inference_backend"] = "dashscope"

    settings = Settings(**settings_kwargs)

    # 加载图片
    print(f"正在加载图片：{args.image}")
    image_bytes = load_image(args.image)
    print(f"图片大小：{len(image_bytes) / 1024:.1f} KB")

    # 创建 Agent
    agent = AgentRunner(settings=settings)

    try:
        detail_level = "brief" if args.brief else "detailed"
        print(f"\n正在进行{('简要' if args.brief else '详细')}识别...")
        print_separator()

        response = await agent.recognize(
            image_bytes,
            detail_level=detail_level,
            question=args.question,
        )

        print_result(response.text, response.objects, response.latency_ms)

    finally:
        await agent.close()


async def run_chat(args: argparse.Namespace) -> None:
    """交互式对话模式。"""
    from app.agent.runner import AgentRunner
    from app.config import Settings

    # 获取 API key
    api_key = args.api_key or os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("错误：需要提供 DASHSCOPE_API_KEY", file=sys.stderr)
        print("  --api-key sk-xxx 或 export DASHSCOPE_API_KEY=sk-xxx", file=sys.stderr)
        sys.exit(1)

    settings_kwargs = {
        "dashscope_api_key": api_key,
        "inference_backend": "dashscope",
    }
    if args.base_url:
        settings_kwargs["dashscope_base_url"] = args.base_url
    if args.model:
        settings_kwargs["agent_model"] = args.model

    settings = Settings(**settings_kwargs)

    # 加载初始图片（如果有）
    image_bytes = None
    if args.image:
        print(f"正在加载图片：{args.image}")
        image_bytes = load_image(args.image)
        print(f"图片大小：{len(image_bytes) / 1024:.1f} KB")

    # 创建 Agent
    agent = AgentRunner(settings=settings)

    print_separator("=")
    print("进入交互式对话模式")
    print("  - 输入消息与 Agent 对话")
    print("  - 输入 'quit' 或 'exit' 退出")
    print("  - 输入 'reset' 重置会话")
    print("  - 输入 'image <path>' 加载新图片")
    print_separator("=")

    session_id = None
    current_image = image_bytes

    try:
        # 如果提供了初始图片，先进行一次识别
        if current_image:
            print("\n[自动识别初始图片]")
            response = await agent.chat(
                "请识别这张图片中的所有物品",
                image_bytes=current_image,
                session_id=session_id,
            )
            session_id = response.session_id
            print_result(response.text, response.objects, response.latency_ms)
            if response.tool_calls:
                print(f"[调用了工具: {', '.join(response.tool_calls)}]")

        while True:
            print()
            try:
                user_input = input("你: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break

            if not user_input:
                continue

            # 处理特殊命令
            if user_input.lower() in ("quit", "exit", "q"):
                print("再见！")
                break

            if user_input.lower() == "reset":
                session_id = None
                current_image = None
                print("[会话已重置]")
                continue

            if user_input.lower().startswith("image "):
                img_path = user_input[6:].strip()
                try:
                    current_image = load_image(img_path)
                    print(f"[已加载图片: {img_path} ({len(current_image) / 1024:.1f} KB)]")
                except SystemExit:
                    print("[加载图片失败]")
                continue

            # 发送给 Agent
            print_separator("-")
            print("Agent: ", end="", flush=True)

            try:
                response = await agent.chat(
                    user_input,
                    image_bytes=current_image,
                    session_id=session_id,
                )
                session_id = response.session_id
                print(response.text)

                if response.tool_calls:
                    print(f"\n[调用了工具: {', '.join(response.tool_calls)}]")
                if response.latency_ms:
                    print(f"[耗时: {response.latency_ms}ms]")

            except Exception as exc:
                print(f"\n[错误: {exc}]")

    finally:
        await agent.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="物品识别 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --image photo.jpg                    # 详细识别
  %(prog)s --image photo.jpg --brief            # 简要识别
  %(prog)s --image photo.jpg --question "..."   # 带问题识别
  %(prog)s --image photo.jpg --chat             # 交互式对话
        """,
    )
    parser.add_argument("--image", "-i", help="图片文件路径")
    parser.add_argument("--brief", "-b", action="store_true", help="简要模式（只列出主要物品）")
    parser.add_argument("--question", "-q", help="附加问题")
    parser.add_argument("--chat", "-c", action="store_true", help="交互式对话模式")
    parser.add_argument("--api-key", help="DashScope API Key（或设置 DASHSCOPE_API_KEY 环境变量）")
    parser.add_argument("--base-url", help="DashScope API Base URL")
    parser.add_argument("--model", default="qwen-vl-plus", help="模型名称 (默认: qwen-vl-plus)")

    args = parser.parse_args()

    # 验证参数
    if not args.image and not args.chat:
        parser.error("需要提供 --image 或使用 --chat 模式")

    if args.chat:
        asyncio.run(run_chat(args))
    else:
        asyncio.run(run_recognize(args))


if __name__ == "__main__":
    main()
