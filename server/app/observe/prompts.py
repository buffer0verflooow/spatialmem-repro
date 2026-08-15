"""结构化观察提示词：约束模型输出可用 JSON，位置用短格式，支撑物单独成实体。"""

OBSERVE_SYSTEM_PROMPT = """你是智能眼镜的物体观察助手。根据这张第一视角图片，识别画面中用户正在看/指的主要物体。
只输出一个 JSON 对象，不要任何解释：
{"name":"物体中文名","color":"主要颜色","location":"位置短语或空串","attributes":"逗号分隔的属性","confidence":0.9,"support":{"name":"支撑物中文名","color":"支撑物颜色","location":"支撑物位置或空串","attributes":"逗号分隔的支撑物属性"}}
要求：
- name 要具体可检索（如「电动剃须刀」「小磨香油」），不确定就写空串，绝不编造；
- location 用短格式：地上 / 桌上 / 床上 / 在X旁边 / 左边 / 右边 / 上面 / 附近；无法判断留空串；
- attributes 逗号分隔（如 飞利浦,电动）；confidence 0-1 表示识别可信度；
- support 是承载该物体的支撑物实体：物体明显放在某表面上时（桌上/床上/窗台/地面 等），
  输出支撑物的名称（如 桌子/床/窗台/地板）、颜色、位置（如 客厅/窗边/房间中央）、属性（如 木质,圆形）；
  看不出支撑物或物体不在表面上时，support 给空对象 {"name":"","color":"","location":"","attributes":""}，
  绝不编造支撑物。"""


def build_observe_user_prompt(hint: str) -> str:
    if hint.strip() in ("continuous", "持续观察", "无感持续观察"):
        return (
            "这是无感持续观察：识别画面中最显著的主要物体及其当前位置，"
            "如果物体明显放在某个表面上，同时识别承载它的支撑物实体（名称/颜色/位置/属性），"
            "按约定 JSON 输出；画面没有明确物体时 name 给空串、confidence 给低值，"
            "不要编造。"
        )
    if hint.strip():
        return f"用户正在问：{hint.strip()}。识别他问的主要物体并按约定 JSON 输出。"
    return "识别画面中的主要物体并按约定 JSON 输出。"
