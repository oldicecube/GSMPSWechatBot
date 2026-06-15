"""
Anima 图像生成服务核心模块
处理提示词转换、workflow 更新、ComfyUI 交互
"""

import json
import os
import time
import random
import glob
import threading
import requests
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANIMA_DIR = os.path.dirname(os.path.abspath(__file__))
WORKFLOW_PATH = os.path.join(ANIMA_DIR, "workflow.json")


class AnimaService:
    """Anima 图像生成服务"""

    # 全局状态锁：确保同时只有一个人在生成
    _lock = threading.Lock()
    _is_generating = False
    _current_user = None

    # ComfyUI 配置
    COMFY_URL = "http://127.0.0.1:8188"
    COMFY_OUTPUT_DIR = None  # 将在 __init__ 时从配置读取
    COMFY_WAIT_TIMEOUT = 60  # 等待超时时间（秒）

    def __init__(self, config=None):
        """初始化服务"""
        self.config = config or {}
        self.comfy_config = config.get("comfy", {})
        
        # 从配置读取 ComfyUI 设置
        self.COMFY_URL = self.comfy_config.get("url", "http://127.0.0.1:8188")
        self.COMFY_OUTPUT_DIR = self.comfy_config.get("output_dir")
        
        # 初始化 DeepSeekProvider
        try:
            from llm.provider.deepseek_provider import DeepSeekProvider
            self.provider = DeepSeekProvider()
        except Exception as e:
            self.provider = None
            print(f"[ANIMA] DeepSeekProvider 初始化失败: {e}")

    @staticmethod
    def can_start_generate(user_wxid: str) -> tuple[bool, str]:
        """
        检查是否可以开始生成
        
        返回: (是否可以生成, 消息)
        """
        with AnimaService._lock:
            if AnimaService._is_generating:
                return False, f"⏳ 已有图像正在生成中...请稍候"
            
            return True, ""

    @staticmethod
    def mark_generating(user_wxid: str) -> bool:
        """标记开始生成"""
        with AnimaService._lock:
            if AnimaService._is_generating:
                return False
            
            AnimaService._is_generating = True
            AnimaService._current_user = user_wxid
            return True

    @staticmethod
    def mark_done():
        """标记生成完成"""
        with AnimaService._lock:
            AnimaService._is_generating = False
            AnimaService._current_user = None

    def convert_prompt_with_deepseek(self, user_prompt: str) -> dict:
        """
        使用 DeepSeek API 将自然语言转换为结构化提示词
        
        返回格式:
        {
            "success": bool,
            "data": {
                "positive_prompt": str,
                "negative_prompt": str,
                "width": int,
                "height": int,
                "cfg": float,
                "steps": int,
                "sampler": str,
                "seed": int
            },
            "error": str (失败时),
            "rejected": bool (如果内容违法)
        }
        """
        if not self.provider:
            return {
                "success": False,
                "error": "DeepSeek 客户端未初始化"
            }

        # 构建系统提示词 - 要求返回 JSON 格式，包含合法性检查
        system_prompt = """你是一个 Anima 图像提示词转换器和内容审核器。

首先检查用户输入是否包含非法、暴力、淫秽或其他不当内容。

JSON 输出格式必须严格如下：

{
"rejected": false,
"rejection_reason": null,
"positive_prompt": "string",
"negative_prompt": "string",
"width": number,
"height": number,
"cfg": number,
"steps": number,
"sampler": "string",
"seed": number
}

或（如果内容违法）：

{
"rejected": true,
"rejection_reason": "具体拒绝原因"
}

一、内容审核规则：

拒绝以下内容：

任何形式的暴力、虐待、伤害他人的内容
明确的性暴力或色情内容
仇恨、歧视、骚扰他人的内容
非法活动相关的内容
伪造文件、证件等欺诈内容
可能涉及对现实人物的攻击、诽谤、隐私侵犯的内容
可能涉及政治、社会热点、新闻事件、现实灾难的内容
黑色幽默、基于现实悲剧的调侃或玩梗内容
其他明显违反法律或现实指向过强的内容
各种莫名其妙和看起来很怪异的内容和要求

二、positive_prompt 规则：

必须按以下结构生成：

质量标签 + 时间标签 + 安全标签 + 主体 + 外观描述 + 动作 + 服饰 + 环境 + 风格补充

固定必须添加基础质量前缀：

masterpiece, best quality, score_7, highres, safe

强化规则（新增）
positive_prompt 必须优先使用英文 tag
所有 tag 必须小写
不使用下划线（score_* 除外）
自然语言必须转换为 danbooru 风格 tag
不确定内容允许“中文+英文补充解释型 tag”，但英文优先
多角色必须明确数量（1girl / 2girls / group）
必须避免现实实体与现实风格绑定
只允许二次元 / 插画 / 动漫风格表达
画师风格规则（强化）
识别到画师风格必须使用 @画师名 格式
必须放在 style 区域
可与其他 style tag 共存
加权规则（新增）

对关键元素自动加权：

主体 / 角色：(1.2–1.6)
风格 / 画师：(1.2–1.5)
核心物品：(1.1–1.3)

示例形式：
(eyjafjalla:1.4), (@anmi:1.3)

三、negative_prompt 固定为：

worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, bad anatomy, extra fingers, missing fingers, text, watermark, logo

四、分辨率规则：

默认全部使用小尺寸：

单人角色 / 头像：768x1024
全身人物：768x1024
场景图：1024x768
默认：768x768

用户没有特殊要求时，一律不超过 1024 像素边长。

五、生成参数默认值：

steps: 32（30–35）
cfg: 4.3（4.0–4.5）
sampler: dpmpp_2m_sde_gpu
seed: -1

六、提示词处理规则：

自然语言必须转换为 Danbooru 风格 tag + 合理视觉补全
信息不足允许补全，但不得改变主体或核心语义
尽量不要偏离二次元 / 动漫 / 插画风格
必须保持视觉一致性与可生成性

七、输出强制要求：

只输出 JSON
JSON 必须可解析
不允许注释
不允许额外文本
不允许 markdown
不允许 trailing comma"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        try:
            response_text = self.provider.send(messages)
            
            # 解析 JSON 响应
            parsed = self._parse_json_response(response_text)
            
            if parsed:
                # 检查是否被拒绝
                if parsed.get("rejected"):
                    return {
                        "success": False,
                        "error": f"内容审核不通过：{parsed.get('rejection_reason', '不适合生成')}",
                        "rejected": True
                    }
                
                return {
                    "success": True,
                    "data": parsed,
                    "rejected": False
                }
            else:
                return {
                    "success": False,
                    "error": "无法解析 DeepSeek JSON 响应"
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"DeepSeek API 调用失败: {e}"
            }

    def _parse_json_response(self, response_text: str) -> dict:
        """
        解析 DeepSeek 返回的 JSON 响应
        
        预期格式（正常）：
        {
            "positive_prompt": "...",
            "negative_prompt": "...",
            "width": 768,
            "height": 1024,
            "cfg": 4.3,
            "steps": 32,
            "sampler": "dpmpp_2m_sde_gpu",
            "seed": -1
        }
        
        或（违法内容）：
        {
            "rejected": true,
            "rejection_reason": "..."
        }
        """
        try:
            import json
            
            # 尝试解析 JSON
            data = json.loads(response_text)
            
            # 检查是否被拒绝
            if isinstance(data, dict) and data.get("rejected"):
                return {
                    "rejected": True,
                    "rejection_reason": data.get("rejection_reason", "内容不符合生成规范")
                }
            
            result = {}
            
            # 验证并提取字段
            if isinstance(data, dict):
                for key in ['positive_prompt', 'negative_prompt', 'width', 'height', 'cfg', 'steps', 'sampler', 'seed']:
                    if key not in data:
                        return None
                    
                    value = data[key]
                    
                    # 类型转换
                    if key in ['positive_prompt', 'negative_prompt', 'sampler']:
                        result[key] = str(value)
                    elif key in ['width', 'height', 'steps', 'seed']:
                        result[key] = int(value)
                    elif key in ['cfg']:
                        result[key] = float(value)
                
                return result if len(result) == 8 else None
            
            return None
        except json.JSONDecodeError as e:
            print(f"[ANIMA] JSON 解析失败: {e}")
            return None
        except Exception as e:
            print(f"[ANIMA] 解析 JSON 响应失败: {e}")
            return None

    def load_workflow(self) -> dict:
        """加载 workflow.json"""
        try:
            with open(WORKFLOW_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[ANIMA] 加载 workflow 失败: {e}")
            return None

    def update_workflow(self, workflow: dict, prompt_data: dict) -> dict:
        """
        更新 workflow 中的提示词和参数
        
        workflow: 原始 workflow 字典
        prompt_data: 包含 positive_prompt, negative_prompt, width, height, cfg, steps, sampler, seed 的字典
        """
        try:
            # 深拷贝 workflow
            updated = json.loads(json.dumps(workflow))
            
            # 更新节点 11（positive_prompt）
            if "11" in updated:
                updated["11"]["inputs"]["text"] = prompt_data["positive_prompt"]
            
            # 更新节点 12（negative_prompt）
            if "12" in updated:
                updated["12"]["inputs"]["text"] = prompt_data["negative_prompt"]
            
            # 更新节点 28（EmptyLatentImage - 宽高）
            if "28" in updated:
                updated["28"]["inputs"]["width"] = prompt_data["width"]
                updated["28"]["inputs"]["height"] = prompt_data["height"]
            
            # 更新节点 19（KSampler - cfg, steps, sampler, seed）
            if "19" in updated:
                updated["19"]["inputs"]["cfg"] = prompt_data["cfg"]
                updated["19"]["inputs"]["steps"] = prompt_data["steps"]
                updated["19"]["inputs"]["sampler_name"] = prompt_data["sampler"]
                # seed 为 -1 时使用随机值
                if prompt_data["seed"] == -1:
                    updated["19"]["inputs"]["seed"] = random.randint(0, 2**63 - 1)
                else:
                    updated["19"]["inputs"]["seed"] = prompt_data["seed"]
            
            return updated
        except Exception as e:
            print(f"[ANIMA] 更新 workflow 失败: {e}")
            return None

    def submit_to_comfyui(self, workflow: dict) -> tuple[bool, str]:
        """
        提交 workflow 到 ComfyUI
        
        返回: (成功, prompt_id 或 错误信息)
        """
        try:
            url = f"{self.COMFY_URL}/prompt"
            response = requests.post(url, json={"prompt": workflow}, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            prompt_id = data.get("prompt_id")
            
            if prompt_id:
                print(f"[ANIMA] 提交成功，prompt_id: {prompt_id}")
                return True, prompt_id
            else:
                return False, "无法获取 prompt_id"
        except Exception as e:
            return False, f"ComfyUI API 调用失败: {e}"

    def wait_for_generation_polling(self, prompt_id: str, max_wait: int = None) -> tuple[bool, int]:
        """
        使用轮询方式等待图像生成完成
        
        返回: (成功, 总等待秒数)
        """
        if max_wait is None:
            max_wait = self.COMFY_WAIT_TIMEOUT
        
        try:
            url = f"{self.COMFY_URL}/history/{prompt_id}"
            poll_interval = 2  # 每2秒轮询一次
            elapsed = 0
            
            print(f"[ANIMA] 轮询等待生成完成 (最长 {max_wait} 秒)...")
            
            while elapsed < max_wait:
                try:
                    response = requests.get(url, timeout=5)
                    response.raise_for_status()
                    data = response.json()
                    
                    # 如果返回了数据表示任务完成
                    if data:
                        print(f"[ANIMA] 生成完成，用时 {elapsed} 秒")
                        return True, elapsed
                    
                except Exception as e:
                    print(f"[ANIMA] 轮询失败: {e}")
                
                # 等待后再轮询
                time.sleep(poll_interval)
                elapsed += poll_interval
            
            # 超时
            print(f"[ANIMA] 轮询超时 (已等待 {max_wait} 秒)")
            return True, max_wait  # 即使超时也继续尝试获取图像
        
        except Exception as e:
            print(f"[ANIMA] 轮询异常: {e}")
            return False, 0

    def get_latest_image(self) -> tuple[bool, str]:
        """
        获取最新生成的图像
        
        返回: (成功, 图像路径 或 错误信息)
        """
        if not self.COMFY_OUTPUT_DIR:
            return False, "ComfyUI 输出目录未配置"
        
        try:
            if not os.path.exists(self.COMFY_OUTPUT_DIR):
                return False, f"输出目录不存在: {self.COMFY_OUTPUT_DIR}"
            
            # 获取最新的 PNG 文件
            files = glob.glob(os.path.join(self.COMFY_OUTPUT_DIR, "*.png"))
            
            if not files:
                return False, "未找到生成的图像"
            
            latest = max(files, key=os.path.getmtime)
            print(f"[ANIMA] 获取最新图像: {latest}")
            return True, latest
        except Exception as e:
            return False, f"获取图像失败: {e}"

    def delete_image(self, image_path: str) -> bool:
        """删除生成的图像文件"""
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
                print(f"[ANIMA] 已删除图像: {image_path}")
                return True
            return False
        except Exception as e:
            print(f"[ANIMA] 删除图像失败: {e}")
            return False

    def generate(self, user_prompt: str) -> dict:
        """
        完整的图像生成流程
        
        返回:
        {
            "success": bool,
            "image_path": str (成功时),
            "error": str (失败时),
            "elapsed_seconds": int (成功时，生成用时)
        }
        """
        start_time = time.time()
        
        try:
            # 1. 将提示词转换为结构化格式
            result = self.convert_prompt_with_deepseek(user_prompt)
            
            if not result.get("success"):
                error = result.get("error", "未知错误")
                return {
                    "success": False,
                    "error": error,
                    "elapsed_seconds": int(time.time() - start_time)
                }
            
            # 检查是否被拒绝
            if result.get("rejected"):
                return {
                    "success": False,
                    "error": result.get("error"),
                    "elapsed_seconds": int(time.time() - start_time),
                    "rejected": True
                }
            
            prompt_data = result["data"]
            
            # 2. 加载并更新 workflow
            workflow = self.load_workflow()
            if not workflow:
                error = "无法加载 workflow"
                return {
                    "success": False,
                    "error": error,
                    "elapsed_seconds": int(time.time() - start_time)
                }
            
            updated_workflow = self.update_workflow(workflow, prompt_data)
            if not updated_workflow:
                error = "更新 workflow 失败"
                return {
                    "success": False,
                    "error": error,
                    "elapsed_seconds": int(time.time() - start_time)
                }
            
            # 3. 提交到 ComfyUI
            success, result_info = self.submit_to_comfyui(updated_workflow)
            if not success:
                return {
                    "success": False,
                    "error": result_info,
                    "elapsed_seconds": int(time.time() - start_time)
                }
            
            prompt_id = result_info
            
            # 4. 轮询等待生成完成
            success, wait_time = self.wait_for_generation_polling(prompt_id)
            
            # 5. 获取生成的图像
            success, image_path = self.get_latest_image()
            if not success:
                return {
                    "success": False,
                    "error": image_path,
                    "elapsed_seconds": int(time.time() - start_time)
                }
            
            elapsed = int(time.time() - start_time)
            
            return {
                "success": True,
                "image_path": image_path,
                "elapsed_seconds": elapsed
            }
        except Exception as e:
            error = f"生成流程异常: {e}"
            return {
                "success": False,
                "error": error,
                "elapsed_seconds": int(time.time() - start_time)
            }
