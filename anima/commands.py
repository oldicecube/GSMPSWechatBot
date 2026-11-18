"""Parse an optional final-line subcommand; never interpret prompt lines as parameters."""
from dataclasses import dataclass, field

MODES = {"文生图":"txt2img", "txt2img":"txt2img", "图生图":"img2img", "img2img":"img2img", "局部修正":"inpaint", "局部重绘":"inpaint", "inpaint":"inpaint", "色彩参考":"color", "color":"color", "控制重绘":"control_inpaint", "control-inpaint":"control_inpaint", "画师混合":"artist_mix", "artist-mix":"artist_mix", "放大":"resize", "resize":"resize", "超分":"upscale", "upscale":"upscale", "gi2":"gi2", "gptimage":"gi2", "gpt-image":"gi2", "gi2img":"gi2_img2img", "图生图gi2":"gi2_img2img", "gi2图生图":"gi2_img2img"}
DISABLED_MODES = frozenset({"inpaint", "color", "control_inpaint"})
ACTIVE_MODES = frozenset(MODES.values()) - DISABLED_MODES
IMAGE_MODES = {"img2img", "resize", "upscale", "gi2_img2img"}
# 走远端 GPT Image 网关的模式：不需要 ComfyUI，图生图版本会等参考图。
GPT_IMAGE_MODES = {"gi2", "gi2_img2img"}
MASK_MODES = {"inpaint", "control_inpaint"}
HELP = """/anima 提示词
回车后仅填写功能选择器：文生图 | 图生图 | 画师混合 | 放大 | 超分 | gi2
不写选择器默认文生图；其他参数自动丢弃。
例：/anima 把外套改成红色
图生图
生成参数由系统选择，种子随机生成。
图片模式随后发图片/动画表情；动画取第一帧，输出单张静态图。
gi2 表示改用 GPT Image（gpt-image-2.6）远端生图；只写 gi2 是文生图，
写「图生图 gi2」则进入图生图并等参考图，两者可同时生效。
局部修正、色彩参考、控制重绘已停用。"""


def ensure_enabled(mode):
    if mode in DISABLED_MODES:
        raise ValueError("局部修正、色彩参考、控制重绘已停用")
    if mode not in ACTIVE_MODES:
        raise ValueError("未知Anima工作模式")


@dataclass
class Request:
    prompt: str
    mode: str = "txt2img"
    options: dict = field(default_factory=dict)

    def payload(self):
        return {"prompt": self.prompt, "mode": self.mode, "options": self.options}

    @classmethod
    def from_payload(cls, value):
        ensure_enabled(value["mode"])
        # Ignore options carried by old or untrusted requests.
        return cls(value["prompt"], value["mode"], {})


def parse_request(content):
    text = str(content or "").replace("\r\n", "\n").strip()
    if text in {"帮助", "help", "--help"}:
        return None
    if not text:
        raise ValueError("请提供提示词，或输入 /anima 帮助")
    lines = text.split("\n")
    prompt = lines[0].strip()
    selectors = []
    for line in lines[1:]:
        for token in line.split():
            key = token.lstrip("/").lower()
            if key in MODES:
                selectors.append(MODES[key])
    disabled = next((mode for mode in selectors if mode in DISABLED_MODES), None)
    if disabled:
        ensure_enabled(disabled)
    # gi2 是"跑在远端网关"的开关，可以和「图生图」叠加：写了图生图就等参考图。
    if "gi2" in selectors:
        mode = "gi2_img2img" if "img2img" in selectors else "gi2"
    else:
        mode = selectors[0] if selectors else "txt2img"
    if not prompt or len(prompt) > 4000:
        raise ValueError("需要1至4000字的用户提示词")
    return Request(prompt, mode, {})
