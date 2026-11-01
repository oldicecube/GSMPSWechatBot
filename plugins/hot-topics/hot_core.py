"""热榜核心逻辑（不依赖 maibot_sdk，可独立测试）。

负责：60s API 多实例抓取（自动切换）、RSS 解析、雷区过滤、兴趣加权、素材格式化。
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote

# 平台注册表：key -> (展示名, 60s API 路径)
SOURCES: dict[str, tuple[str, str]] = {
    "weibo": ("微博热搜", "/weibo"),
    "zhihu": ("知乎热榜", "/zhihu"),
    "douyin": ("抖音热点", "/douyin"),
    "toutiao": ("头条热榜", "/toutiao"),
    "bili": ("B站热搜", "/bili"),
    "baidu_hot": ("百度热搜", "/baidu/hot"),
    "baidu_tieba": ("贴吧话题榜", "/baidu/tieba"),
    "baidu_teleplay": ("百度电视剧榜", "/baidu/teleplay"),
    "it_news": ("IT之家热榜", "/it-news"),
    "dongchedi": ("懂车帝热搜", "/dongchedi"),
    "quark": ("夸克热点", "/quark"),
}

# 平台别名（中文/简写 -> key）
ALIASES: dict[str, str] = {
    "微博": "weibo", "微博热搜": "weibo",
    "知乎": "zhihu", "知乎热榜": "zhihu",
    "抖音": "douyin", "抖音热点": "douyin",
    "头条": "toutiao", "头条热榜": "toutiao",
    "b站": "bili", "B站": "bili", "哔哩哔哩": "bili", "bilibili": "bili", "bili": "bili",
    "百度": "baidu_hot", "百度热搜": "baidu_hot",
    "贴吧": "baidu_tieba", "贴吧话题": "baidu_tieba", "百度贴吧": "baidu_tieba",
    "电视剧": "baidu_teleplay", "追剧": "baidu_teleplay",
    "it": "it_news", "IT之家": "it_news", "it之家": "it_news", "数码": "it_news",
    "汽车": "dongchedi", "懂车帝": "dongchedi",
    "夸克": "quark",
    "all": "all", "全部": "all", "热点": "all", "热榜": "all", "热搜": "all",
}

DEFAULT_BASE_URLS = [
    "https://60s.crystelf.top",
    "https://60s.7se.cn",
    "https://60s.viki.moe",
]

# 内置默认雷区（用户可在配置中增删，配置生效后优先级最高）
DEFAULT_BLOCKLIST = ["赌", "色情", "毒品"]

_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL_SECONDS = 600  # 10 分钟缓存，避免高频打接口

_UA = "Mozilla/5.0 MaiBotHotTopics/0.1"


def resolve_platform(name: str) -> str | None:
    """把用户/LLM 传来的平台名解析成标准 key，无法识别返回 None。"""
    key = (name or "").strip()
    if not key:
        return None
    if key in SOURCES:
        return key
    if key in ALIASES:
        return ALIASES[key]
    lower = key.lower()
    if lower in ALIASES:
        return ALIASES[lower]
    if lower in SOURCES:
        return lower
    return None


def _http_get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _pick_items(payload: dict) -> list[dict]:
    """从 60s API 的多种返回结构里提取统一条目列表。"""
    data = payload.get("data")
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list) and value:
                data = value
                break
    if not isinstance(data, list):
        return []
    items: list[dict] = []
    for raw in data[:60]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("name") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title,
                "hot": raw.get("hot_value") or raw.get("score") or raw.get("hot"),
                "url": raw.get("url") or raw.get("link") or "",
            }
        )
    return items


def fetch_platform(
    platform_key: str,
    base_urls: list[str] | None = None,
    timeout: float = 8.0,
    use_cache: bool = True,
) -> list[dict]:
    """抓取单个平台榜单，多实例自动切换，全部失败抛 RuntimeError。"""
    if platform_key not in SOURCES:
        raise ValueError(f"未知平台: {platform_key}")
    cache_key = f"60s:{platform_key}"
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    path = SOURCES[platform_key][1]
    urls = [u.rstrip("/") for u in (base_urls or DEFAULT_BASE_URLS) if u.strip()]
    last_error: Exception | None = None
    for base in urls:
        try:
            payload = _http_get_json(f"{base}/v2{path}", timeout)
            # 60s 实例既有 code=200，也有 status=200 的成功信封。
            # 显式 code 错误不能被另一个 status 字段覆盖。
            status = payload.get("code") if "code" in payload else payload.get("status")
            if status != 200:
                raise RuntimeError(f"接口返回 code/status={status}")
            items = _pick_items(payload)
            if not items:
                raise RuntimeError("返回数据为空")
            _CACHE[cache_key] = (time.time(), items)
            return items
        except Exception as exc:  # noqa: BLE001 - 多实例切换必须吞掉单实例错误
            last_error = exc
            continue
    raise RuntimeError(f"所有实例抓取失败（{platform_key}）: {last_error}")


def search_deepseek(
    query: str,
    api_key: str,
    base_url: str = "https://api.deepseek.com/anthropic",
    model: str = "deepseek-v4-flash",
    max_uses: int = 2,
    timeout: float = 60.0,
) -> str:
    """调用 DeepSeek Anthropic 兼容接口联网搜索，返回要点文本。

    供"订阅查询词"使用：每轮漫步随机挑一个关注话题全网搜一轮。
    纯标准库实现，无第三方依赖。
    """
    if not api_key:
        raise RuntimeError("未配置 DeepSeek API 密钥")
    url = base_url.rstrip("/") + "/v1/messages"
    if not re.match(r"^https://", url):  # 只允许 HTTPS（接口带密钥，防降级/误配）
        raise RuntimeError(f"接口地址必须为 HTTPS：{base_url[:60]}")
    body = {
        "model": model,
        "max_tokens": 2000,
        "system": (
            "你是话题动态检索助手。请检索该话题的近期动态和热门讨论，"
            "也可以提及相关的背景内容。用要点列表返回，每条一行，"
            "尽量为每条注明日期（如 9月2日 或 2026-09-02）。"
        ),
        "messages": [{"role": "user", "content": query}],
        "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    text_parts = []
    for block in data.get("content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            text_parts.append(str(block["text"]).strip())
    return "\n".join(t for t in text_parts if t).strip()


_DATE_PATTERNS = [
    re.compile(r"(?:(20\d{2})[-/年.])?\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?"),
]


def extract_line_date(text: str, today=None):
    """从一行文本里解析日期，返回 date 对象；解析不到返回 None。

    支持三种形态：2026-09-02 / 2026年9月2日 / 9月2日（补当前年份，
    且跨年时自动取更合理的年份）。
    """
    import datetime

    today = today or datetime.date.today()
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        year = int(m.group(1)) if m.group(1) else today.year
        try:
            d = datetime.date(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if not m.group(1) and (today - d).days > 180:
            try:
                d = datetime.date(year - 1, d.month, d.day)
            except ValueError:
                pass
        return d
    return None


def interest_strict_filter(items: list[dict], interests: list[str]) -> list[dict]:
    """全局白名单：只保留命中兴趣词的条目（严格模式用，interests 为空则原样返回）。"""
    if not interests:
        return items
    return [i for i in items if _contains_any(_matchable_text(i), interests)]


def remove_by_keywords(titles: list[str], keywords: list[str]) -> list[str]:
    """按关键词剔除标题（聊天黑名单用）：keywords 为空则原样返回。"""
    if not keywords:
        return titles
    return [t for t in titles if not _contains_any(t, keywords)]


def filter_by_keywords(titles: list[str], keywords: list[str]) -> list[str]:
    """按关键词过滤标题列表（群聊内容定制用）：keywords 为空则原样返回。"""
    if not keywords:
        return titles
    return [t for t in titles if _contains_any(t, keywords)]


def filter_by_freshness(
    titles: list[str],
    max_age_days: int,
    today=None,
) -> tuple[list[str], int]:
    """按日期过滤"刷到"的条目：无日期的保留（无法判断），超龄的丢弃。

    返回 (保留的条目, 被丢弃的超龄条数)。
    """
    import datetime

    today = today or datetime.date.today()
    if max_age_days <= 0:
        return titles, 0
    kept: list[str] = []
    dropped = 0
    for t in titles:
        d = extract_line_date(t, today)
        if d is None or (today - d).days <= max_age_days:
            kept.append(t)
        else:
            dropped += 1
    return kept, dropped


def _contains_any(text: str, keywords: list[str]) -> str | None:
    lowered = text.lower()
    for kw in keywords:
        kw = (kw or "").strip().lower()
        if kw and kw in lowered:
            return kw
    return None


def _matchable_text(item: dict) -> str:
    """拼接可用于关键词匹配的文本：标题 + 摘要（如有）。"""
    desc = str(item.get("desc") or "").strip()
    return item["title"] + (" " + desc if desc else "")


def random_sample(
    items: list[dict],
    n: int,
    skip_top: int = 3,
) -> list[dict]:
    """随机漫步采样：跳过榜单头部（人人都看得到的），从余下部分纯随机抽 n 条。

    模拟真人"刷到"——不是看榜首，而是偶尔撞见中腰部内容。
    """
    pool = items[skip_top:] if len(items) > skip_top else []
    if not pool:
        pool = items
    n = max(1, min(n, len(pool)))
    return random.sample(pool, n)


def count_interest_hits(items: list[dict], interests: list[str]) -> list[dict]:
    """返回命中兴趣关键词的条目，便于验证加权是否生效。"""
    return [i for i in items if _contains_any(_matchable_text(i), interests)]


def format_ambient(items: list[dict], source_label: str) -> str:
    """把"刷到"的内容格式化为低存在感的记忆片段（带摘要更有料）。"""
    lines = [f"（{source_label}刷到的）"]
    for item in items:
        entry = f"- {item['title']}"
        if item.get("desc"):
            entry += f"（{item['desc']}）"
        lines.append(entry)
    return "\n".join(lines)


def blocklist_filter(items: list[dict], blocklist: list[str]) -> tuple[list[dict], int]:
    """仅按雷区过滤（供随机漫步等不走完整排序的路径使用）。

    返回 (保留的条目, 拦截数量)。
    """
    kept: list[dict] = []
    blocked = 0
    for item in items:
        if _contains_any(_matchable_text(item), blocklist):
            blocked += 1
        else:
            kept.append(item)
    return kept, blocked


def filter_and_rank(
    items: list[dict],
    blocklist: list[str],
    interests: list[str],
    max_items: int = 10,
    strict: bool = False,
) -> tuple[list[dict], list[dict]]:
    """雷区过滤（硬性，最高优先级）+ 兴趣加权排序。

    strict=False（默认）：兴趣命中置顶，其余保留——真人刷热搜"都扫一眼"。
    strict=True：只保留兴趣命中的条目——适合"只聊她关心的"场景。
    返回 (过滤排序后的条目, 被雷区拦截的条目)。
    """
    blocked: list[dict] = []
    kept: list[dict] = []
    for item in items:
        hit = _contains_any(_matchable_text(item), blocklist)
        if hit:
            item = {**item, "blocked_by": hit}
            blocked.append(item)
        else:
            kept.append(item)

    if strict and interests:
        kept = [i for i in kept if _contains_any(_matchable_text(i), interests)]

    def interest_rank(entry: dict) -> int:
        return 0 if _contains_any(_matchable_text(entry), interests) else 1

    kept.sort(key=interest_rank)  # 稳定排序：兴趣命中的排前，其余保持榜单原序
    return kept[:max_items], blocked


def format_material(
    source_label: str,
    items: list[dict],
    interests: list[str],
    blocked_count: int = 0,
) -> str:
    """把榜单格式化为素材式输出（给 Replyer 用角色口吻转述，禁止直接照搬）。"""
    lines = [f"【{source_label}】共 {len(items)} 条（按你的兴趣排过序）："]
    for i, item in enumerate(items, 1):
        star = ""
        if _contains_any(_matchable_text(item), interests):
            star = " ★你的兴趣"
        hot = f"（热度{item['hot']}）" if item.get("hot") else ""
        lines.append(f"{i}. {item['title']}{hot}{star}")
    if blocked_count:
        lines.append(f"（另有 {blocked_count} 条被雷区规则拦截，不要提及）")
    lines.append("以上是素材，不是台词——请用角色自己的口吻转述感兴趣的部分。")
    return "\n".join(lines)
