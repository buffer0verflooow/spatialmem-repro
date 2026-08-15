#!/usr/bin/env python3
"""独立的物品识别测试脚本（兼容 Python 3.9+）。

直接调用 DashScope API 进行测试，不依赖 app 模块。
"""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

# 检查 httpx
try:
    import httpx
except ImportError:
    print("需要安装 httpx: pip install httpx")
    sys.exit(1)


# Agent 提示词
AGENT_SYSTEM_PROMPT = """你是一个专业的视觉识别助手，擅长分析图像并识别其中的物品。

你的主要能力：
1. **物品识别**：准确识别图像中的各类物品
2. **场景理解**：分析整体环境
3. **知识增强**：可以检索知识库获取物品的专业信息

回答要求：
- 使用中文回答，物品名称同时提供中英文
- 描述物品位置时使用相对位置
- 对于不确定的物品，说明不确定性程度"""


RECOGNIZE_DETAILED_PROMPT = """请仔细分析这张图片，识别所有可见的物品。

对每个物品，请提供：
1. **名称**：中文名称和英文名称
2. **位置**：在画面中的相对位置
3. **数量**：如果是多个同类物品
4. **外观**：颜色、材质、大致尺寸
5. **状态**：物品的当前状态

请按物品在画面中的显著程度排序。"""


RECOGNIZE_BRIEF_PROMPT = """请快速列出这张图片中可见的主要物品（3-5 个最显眼的）。

格式：物品名（英文）- 位置"""


async def recognize_image(
    image_path: str,
    api_key: str,
    *,
    detail_level: str = "detailed",
    question: str = None,
    model: str = "qwen-vl-plus",
) -> dict:
    """调用 DashScope API 进行物品识别。"""

    # 读取并编码图片
    image_bytes = Path(image_path).read_bytes()
    image_b64 = base64.b64encode(image_bytes).decode()

    # 构建提示词
    prompt = RECOGNIZE_DETAILED_PROMPT if detail_level == "detailed" else RECOGNIZE_BRIEF_PROMPT
    if question:
        prompt = f"{prompt}\n\n用户附加问题：{question}"

    # 构建请求
    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                },
                {"type": "text", "text": prompt},
            ],
        },
    ]

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 2000,
        "temperature": 0.3,
    }

    # 调用 API
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    start = time.perf_counter()

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )

    latency_ms = int((time.perf_counter() - start) * 1000)

    if resp.status_code != 200:
        return {
            "success": False,
            "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
            "latency_ms": latency_ms,
        }

    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})

    # 提取物品列表（支持多种格式）
    objects = []
    import re
    # 格式1: **物品名 (English Name)**
    pattern1 = r"\*\*([^*]+?)\s*\(([^)]+)\)\*\*"
    # 格式2: ### N. 物品名 (English Name)
    pattern2 = r"###\s*\d+\.\s*(.+?)\s*\(([^)]+)\)"
    # 格式3: N. 物品名 (English Name) - 位置
    pattern3 = r"\d+\.\s*(.+?)\s*\(([^)]+)\)\s*-"

    matches = re.findall(pattern1, text) or re.findall(pattern2, text) or re.findall(pattern3, text)
    for cn_name, en_name in matches:
        objects.append({"name": cn_name.strip(), "name_en": en_name.strip()})

    return {
        "success": True,
        "text": text,
        "objects": objects,
        "latency_ms": latency_ms,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "model": data.get("model", model),
    }


def print_report(results: list, image_path: str) -> str:
    """生成测试报告。"""
    lines = []
    lines.append("=" * 70)
    lines.append("物品识别 Agent 测试报告")
    lines.append("=" * 70)
    lines.append(f"测试图片: {image_path}")
    lines.append(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"测试用例数: {len(results)}")
    lines.append("")

    passed = sum(1 for r in results if r.get("success"))
    failed = len(results) - passed

    lines.append("-" * 70)
    lines.append("测试摘要")
    lines.append("-" * 70)
    lines.append(f"  通过: {passed}")
    lines.append(f"  失败: {failed}")
    lines.append(f"  通过率: {passed/len(results)*100:.1f}%")
    lines.append("")

    for i, result in enumerate(results, 1):
        lines.append("-" * 70)
        lines.append(f"测试用例 {i}: {result.get('name', 'N/A')}")
        lines.append("-" * 70)

        if result.get("success"):
            lines.append(f"  状态: PASS")
            lines.append(f"  模型: {result.get('model', 'N/A')}")
            lines.append(f"  延迟: {result.get('latency_ms', 0)}ms")
            lines.append(f"  Token 消耗: prompt={result.get('prompt_tokens', 0)}, "
                        f"completion={result.get('completion_tokens', 0)}, "
                        f"total={result.get('total_tokens', 0)}")
            lines.append(f"  检测到物品数: {len(result.get('objects', []))}")

            if result.get("objects"):
                lines.append("  物品列表:")
                for obj in result["objects"]:
                    lines.append(f"    - {obj['name']} ({obj.get('name_en', '')})")

            lines.append("")
            lines.append("  识别结果:")
            # 缩进输出
            for line in result.get("text", "").split("\n"):
                lines.append(f"    {line}")
        else:
            lines.append(f"  状态: FAIL")
            lines.append(f"  错误: {result.get('error', 'Unknown error')}")

        lines.append("")

    # 性能统计
    latencies = [r["latency_ms"] for r in results if r.get("success")]
    if latencies:
        lines.append("-" * 70)
        lines.append("性能统计")
        lines.append("-" * 70)
        latencies.sort()
        lines.append(f"  最快: {min(latencies)}ms")
        lines.append(f"  最慢: {max(latencies)}ms")
        lines.append(f"  平均: {sum(latencies)//len(latencies)}ms")
        if len(latencies) >= 2:
            p50 = latencies[len(latencies)//2]
            p95 = latencies[int(len(latencies)*0.95)]
            lines.append(f"  P50: {p50}ms")
            lines.append(f"  P95: {p95}ms")
        lines.append("")

    # Token 统计
    total_prompt = sum(r.get("prompt_tokens", 0) for r in results if r.get("success"))
    total_completion = sum(r.get("completion_tokens", 0) for r in results if r.get("success"))
    if total_prompt or total_completion:
        lines.append("-" * 70)
        lines.append("Token 统计")
        lines.append("-" * 70)
        lines.append(f"  Prompt tokens: {total_prompt}")
        lines.append(f"  Completion tokens: {total_completion}")
        lines.append(f"  Total tokens: {total_prompt + total_completion}")
        lines.append("")

    lines.append("=" * 70)
    lines.append("报告结束")
    lines.append("=" * 70)

    return "\n".join(lines)


async def main():
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("错误: 请设置 DASHSCOPE_API_KEY 环境变量")
        sys.exit(1)

    image_path = "tests/fixtures/test_desk.jpg"
    if len(sys.argv) > 1:
        image_path = sys.argv[1]

    if not Path(image_path).exists():
        print(f"错误: 图片不存在 {image_path}")
        sys.exit(1)

    print(f"测试图片: {image_path}")
    print(f"图片大小: {Path(image_path).stat().st_size / 1024:.1f} KB")
    print()

    results = []

    # 测试用例 1: 详细识别
    print("正在执行测试 1/3: 详细识别...")
    result = await recognize_image(image_path, api_key, detail_level="detailed")
    result["name"] = "详细识别 (detailed)"
    results.append(result)
    if result["success"]:
        print(f"  完成: {result['latency_ms']}ms, 检测到 {len(result['objects'])} 个物品")
    else:
        print(f"  失败: {result['error'][:100]}")

    # 测试用例 2: 简要识别
    print("正在执行测试 2/3: 简要识别...")
    result = await recognize_image(image_path, api_key, detail_level="brief")
    result["name"] = "简要识别 (brief)"
    results.append(result)
    if result["success"]:
        print(f"  完成: {result['latency_ms']}ms, 检测到 {len(result['objects'])} 个物品")
    else:
        print(f"  失败: {result['error'][:100]}")

    # 测试用例 3: 带问题识别
    print("正在执行测试 3/3: 带问题识别...")
    result = await recognize_image(
        image_path, api_key,
        question="这张图片中最贵的物品可能是什么？"
    )
    result["name"] = "带问题识别 (with question)"
    results.append(result)
    if result["success"]:
        print(f"  完成: {result['latency_ms']}ms, 检测到 {len(result['objects'])} 个物品")
    else:
        print(f"  失败: {result['error'][:100]}")

    # 生成报告
    print()
    report = print_report(results, image_path)
    print(report)

    # 保存报告到文件
    report_path = "tests/fixtures/agent_test_report.txt"
    Path(report_path).write_text(report, encoding="utf-8")
    print(f"\n报告已保存到: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
