import json
import os
import random
from datetime import datetime
from openai import OpenAI

from llm.config import get_api_key, get_llm_config
from utils.points_manager import get_points, add_points
from utils.sqlite_store import (
    load_document, player_stats_all, player_stats_get,
    player_stats_top, player_stats_update,
)

COMMAND = "/player"

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
FORTUNE_WORDS_FILE = os.path.join(BASE_DIR, "data", "fortune_words.json")
LIFE_WORDS_FILE = os.path.join(BASE_DIR, "data", "life_words.json")

# Legacy fallback for records created before `first_join_at` was tracked.
LEGACY_BASE_TIME = datetime(2026, 4, 13)


def init(config):
    """初始化插件"""
    pass


def parse_first_join_at(stats):
    raw_value = stats.get("first_join_at")
    if not raw_value:
        return LEGACY_BASE_TIME

    try:
        return datetime.fromisoformat(str(raw_value))
    except (TypeError, ValueError):
        return LEGACY_BASE_TIME


def calc_average_days(stats):
    first_join_at = parse_first_join_at(stats)
    today = datetime.now().date()
    first_day = first_join_at.date()
    return max(1, (today - first_day).days + 1)


def build_player_text(player, stats):
    total = stats.get("total_time", 0)
    if not isinstance(total, (int, float)):
        total = 0

    days = calc_average_days(stats)
    total_h = total / 3600
    daily_h = (total / days) / 3600

    return (
        f"玩家 {player} 信息:\n\n"
        f"累计在线: {total_h:.2f} 小时\n"
        f"日均在线: {daily_h:.2f} 小时"
    )


def build_bound_player_text(player, stats, wxid=None):
    total = stats.get("total_time", 0)
    if not isinstance(total, (int, float)):
        total = 0

    days = calc_average_days(stats)
    total_h = total / 3600
    daily_h = (total / days) / 3600
    
    text = (
        f"当前绑定玩家: {player}\n\n"
        f"累计在线: {total_h:.2f} 小时\n"
        f"日均在线: {daily_h:.2f} 小时"
    )
    
    # 添加积分显示（如果有wxid）
    if wxid:
        points = get_points(wxid)
        text += f"\n💎 积分: {points:.1f}"
    
    return text


def build_fortune_text(player_name):
    """生成 --fortune 命令的运势文本"""
    words = load_document(FORTUNE_WORDS_FILE, default={})
    
    if not isinstance(words, dict) or not words:
        return "运势字库不存在或为空"

    blocks = words.get("方块", [])
    mobs = words.get("生物", [])
    actions = words.get("行为", [])

    if not blocks or not mobs or not actions:
        return "运势字库不完整"

    lucky_block = random.choice(blocks)
    unlucky_block = random.choice(blocks)
    lucky_mob = random.choice(mobs)
    unlucky_mob = random.choice(mobs)
    lucky_action = random.choice(actions)
    unlucky_action = random.choice(actions)
    fortune_index = random.randint(0, 100)

    filled_stars = random.randint(0, 5)
    empty_stars = 5 - filled_stars
    star_display = "★" * filled_stars + "☆" * empty_stars

    return (
        f"🎲 {player_name}今日运势\n"
        f"运势：{star_display}\n"
        f"-\n"
        f"幸运方块：{lucky_block}\n"
        f"倒霉方块：{unlucky_block}\n"
        f"幸运生物：{lucky_mob}\n"
        f"倒霉生物：{unlucky_mob}\n"
        f"-\n"
        f"今日宜：{lucky_action}\n"
        f"今日不宜：{unlucky_action}\n"
        f"-\n"
        f"幸运指数：{fortune_index}"
    )


def build_value_template(player_name):
    """生成 --value 命令的估值模板（不含 LLM 锐评）"""
    iron_count = random.randint(1, 64)

    lines = [
        f"💰 {player_name} 当前估值\n-",
        f"{iron_count}个铁锭",
    ]

    # 60% 概率显示绿宝石
    if random.random() < 0.6:
        emerald_count = random.randint(1, 64)
        lines.append(f"{emerald_count}个绿宝石")

    # 10% 概率显示钻石
    if random.random() < 0.1:
        diamond_count = random.randint(1, 64)
        lines.append(f"{diamond_count}个钻石")

    # 20% 概率：只显示腐肉
    if random.random() < 0.2:
        lines = [
            f"💰 {player_name} 当前估值",
            f"-\n{random.randint(1, 64)}个腐肉",
        ]

    # 村民锐评占位符
    lines.append("-\n村民锐评：{comment}")

    return "\n".join(lines)


def build_life_template(player_name):
    """生成 --life 命令的转生模板（不含 LLM 锐评）"""
    words = load_document(LIFE_WORDS_FILE, default={})
    
    if not isinstance(words, dict) or not words:
        return "LIFE 字库不存在或为空"

    results = words.get("转生结果", {})
    if not isinstance(results, dict) or not results:
        return "LIFE 字库不完整"

    result = random.choice(list(results.keys()))
    abilities = results.get(result, [])
    if not abilities:
        return "LIFE 字库不完整"

    ability = random.choice(abilities)

    lines = [
        f"🔄 {player_name} 转生成功",
        "-",
        "转生结果：",
        f"{result}",
        "",
        "特殊能力：",
        f"{ability}",
        "-",
        "村民锐评：{comment}",
    ]

    return "\n".join(lines)


def fetch_value_comment(template_text):
    """绕过原有 LLM 机制，直接调用 API 获取村民锐评"""
    try:
        llm_config = get_llm_config()
        api_key = get_api_key()
        model = llm_config.get("model", "deepseek-chat")
    except Exception as e:
        print(f"[VALUE LLM ERROR] 获取 LLM 配置失败: {e}")
        return "你的价值已经让我无言以对了"

    prompt = (
        "你是一个MC服内的投资村民NPC，请基于以下内容给出你的毒舌投资锐评，但是很值钱的时候该夸就夸，毒舌版（铁锭100%概率，绿宝石60%概率，钻石10%概率，腐肉有20%概率单独显示或80概率不显示，价值等同于破烂。随机数量（即价值上下限）为每类0-64个。为仅供价值参考）：\n"
        + template_text
        + "\n不要回复其他内容，只回复锐评，限制在20字以内或左右"
    )

    messages = [
        {"role": "user", "content": prompt},
    ]

    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
        comment = response.choices[0].message.content
        return str(comment or "").strip()
    except Exception as e:
        print(f"[VALUE LLM ERROR] API 调用失败: {e}")
        return "你的价值已经让我无言以对了"


def fetch_life_comment(template_text):
    """绕过原有 LLM 机制，直接调用 API 获取村民锐评"""
    try:
        llm_config = get_llm_config()
        api_key = get_api_key()
        model = llm_config.get("model", "deepseek-chat")
    except Exception as e:
        print(f"[LIFE LLM ERROR] 获取 LLM 配置失败: {e}")
        return "你转生的结果连我都看不懂"

    prompt = (
        "你是Minecraft服务器里的毒舌村民。\n\n"
        "玩家转生结果：\n\n"
        + template_text
        + "\n\n请生成一句吐槽。\n\n要求：\n\n* 只输出锐评\n* 不超过20字\n* 阴阳怪气\n* 搞笑\n* 不解释\n* 不加引号\n* 不加前缀\n* 像老玩家吐槽群友"
    )

    messages = [
        {"role": "user", "content": prompt},
    ]

    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
        comment = response.choices[0].message.content
        return str(comment or "").strip()
    except Exception as e:
        print(f"[LIFE LLM ERROR] API 调用失败: {e}")
        return "你转生的结果连我都看不懂"


def handle(content, context):
    wxid = context.get("wxid") or context.get("raw", {}).get("wxid")

    if not wxid:
        return "无法获取用户信息"

    wxid = str(wxid).strip()
    
    # 转换content为字符串
    if isinstance(content, list):
        content = content[0] if content else ""
    
    content_str = str(content).strip() if content else ""

    data = player_stats_all()
    if not data:
        return "暂无数据"

    if not content_str:
        for player, stats in data.items():
            if not isinstance(stats, dict):
                continue

            bind_user = stats.get("bind_user")
            if not isinstance(bind_user, str):
                continue

            if bind_user.strip() != wxid:
                continue

            return build_bound_player_text(player, stats, wxid)

        return "你还未绑定任何玩家"

    # --top 子命令：获取总在线时长前5名
    if content_str == "--top":
        top_players = player_stats_top(5)

        if not top_players:
            return "暂无玩家数据"

        lines = ["🏆 在线时长排行 TOP5:\n"]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, (name, stats) in enumerate(top_players):
            total = stats.get("total_time", 0)
            total_h = total / 3600
            lines.append(f"{medals[i]} {name}: {total_h:.2f} 小时")

        return "\n".join(lines)

    # --fortune 子命令
    if content_str == "--fortune":
        # 查找当前绑定的玩家
        player_name = None
        player_stats = None
        for player, stats in data.items():
            if not isinstance(stats, dict):
                continue
            bind_user = stats.get("bind_user")
            if isinstance(bind_user, str) and bind_user.strip() == wxid:
                player_name = player
                player_stats = stats
                break

        if player_name is None:
            return "你还未绑定玩家，请先绑定玩家"

        # 检查今日是否已抽取
        last_str = player_stats.get("last_fortune_at")
        if last_str:
            try:
                last_date = datetime.fromisoformat(str(last_str)).date()
                if last_date == datetime.now().date():
                    return "你今日已抽取过运势"
            except (TypeError, ValueError):
                pass

        # 生成运势
        fortune_text = build_fortune_text(player_name)

        # 获取随机积分（5-10）
        points_gained = random.randint(5, 10)
        add_points(wxid, points_gained)
        total_points = get_points(wxid)

        # 写入最后抽取时间
        player_stats_update(player_name, last_fortune_at=datetime.now().isoformat())

        # 拼接最后的响应，包含积分获得信息
        fortune_text = fortune_text.replace(
            "幸运指数：",
            f"💎 获得积分: +{points_gained} (总计: {total_points:.1f})\n-\n幸运指数："
        )

        return fortune_text

    # --value 子命令
    if content_str == "--value":
        # 查找当前绑定的玩家
        player_name = None
        for player, stats in data.items():
            if not isinstance(stats, dict):
                continue
            bind_user = stats.get("bind_user")
            if isinstance(bind_user, str) and bind_user.strip() == wxid:
                player_name = player
                break

        if player_name is None:
            return "你还未绑定玩家，请先绑定玩家"

        # 生成估值模板
        template = build_value_template(player_name)

        # 绕过原有 LLM 机制直接获取村民锐评
        comment = fetch_value_comment(template)

        # 拼接待发送的最终文本（把 {comment} 占位符替换为实际锐评）
        result = template.replace("{comment}", comment)

        return result

    if content_str == "--life":
        # 查找当前绑定的玩家
        player_name = None
        for player, stats in data.items():
            if not isinstance(stats, dict):
                continue
            bind_user = stats.get("bind_user")
            if isinstance(bind_user, str) and bind_user.strip() == wxid:
                player_name = player
                break

        if player_name is None:
            return "你还未绑定玩家，请先绑定玩家"

        # 生成转生模板
        template = build_life_template(player_name)

        # 绕过原有 LLM 机制直接获取村民锐评
        comment = fetch_life_comment(template)

        # 拼接待发送的最终文本（把 {comment} 占位符替换为实际锐评）
        result = template.replace("{comment}", comment)

        return result

    player = content_str
    stats = player_stats_get(player)

    if not stats:
        return f"玩家 {player} 无数据"

    return build_player_text(player, stats)
