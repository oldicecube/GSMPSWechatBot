"""Anima Base multi-mode service backed by ComfyUI's API."""
import io
import json
import logging
import math
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image

from anima.commands import Request, IMAGE_MODES, MASK_MODES, ensure_enabled
from anima.prompting import convert
from anima.workflows import build_graph
from anima.safety import review_image

LOG = logging.getLogger(__name__)
ANIMA_DIR = Path(__file__).resolve().parent
WORKFLOW_PATH = ANIMA_DIR / "workflow.json"


class AnimaService:
    _lock = threading.Lock()
    _is_generating = False
    _current_user = None
    COMFY_URL = "http://127.0.0.1:8188"
    COMFY_WAIT_TIMEOUT = 180

    def __init__(self, config=None):
        self.config = config or {}
        self.comfy_config = self.config.get("comfy", {})
        self.anima_config = dict(self.config.get("anima", {}))
        self.COMFY_URL = self.comfy_config.get("url", self.COMFY_URL).rstrip("/")
        self.COMFY_OUTPUT_DIR = self.comfy_config.get("output_dir") or r"H:\Anima\ComfyUI\output"
        self.COMFY_WAIT_TIMEOUT = self._positive_timeout(self.anima_config.get("generation_timeout",180))
        self.output_cache = Path(tempfile.gettempdir()) / "wechat-anima"
        self._schema = None
        self._schema_time = 0
        self._last_error = ""
        try:
            from llm.provider.deepseek_provider import DeepSeekProvider
            self.provider = DeepSeekProvider()
        except Exception as exc:
            self.provider = None
            LOG.warning("Prompt provider unavailable: %s",type(exc).__name__)

    @staticmethod
    def _positive_timeout(value):
        value = float(value)
        if not math.isfinite(value) or not 0 < value <= 600: raise ValueError("等待上限须为0至600秒")
        return value

    def configure_connection(self, url, generation_timeout=None):
        url = str(url or "").strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("ComfyUI地址必须为不含凭据或查询参数的HTTP(S)地址")
        if self.COMFY_URL != url:
            self._schema = None
        self.COMFY_URL = url
        if generation_timeout is not None: self.COMFY_WAIT_TIMEOUT = self._positive_timeout(generation_timeout)

    def check_connection(self):
        try:
            response = requests.get(self.COMFY_URL+"/system_stats",timeout=5)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data,dict) or not isinstance(data.get("devices"),list): raise ValueError("Not ComfyUI")
            return True,""
        except (requests.RequestException,ValueError):
            return False,"无法连接ComfyUI，请检查绘图电脑和服务地址"

    @staticmethod
    def can_start_generate(user_wxid):
        with AnimaService._lock:
            return (False,"⏳ 已有图像正在生成，请稍候") if AnimaService._is_generating else (True,"")

    @staticmethod
    def mark_generating(user_wxid):
        with AnimaService._lock:
            if AnimaService._is_generating: return False
            AnimaService._is_generating = True
            AnimaService._current_user = user_wxid
            return True

    @staticmethod
    def mark_done():
        with AnimaService._lock:
            AnimaService._is_generating = False
            AnimaService._current_user = None

    def object_info(self):
        if self._schema is None or time.monotonic()-self._schema_time > 30:
            response=requests.get(self.COMFY_URL+"/object_info",timeout=15)
            response.raise_for_status()
            self._schema=response.json()
            if not isinstance(self._schema,dict):raise ValueError("无法读取ComfyUI节点定义")
            self._schema_time=time.monotonic()
        return self._schema

    def preflight(self,spec):
        ensure_enabled(spec.mode)
        placeholder={"positive_prompt":"test","negative_prompt":"","artists":["a","b"],"aspect":None}
        build_graph(spec,placeholder,self.anima_config,self.object_info(),
                    image_name="check.png" if spec.mode in IMAGE_MODES else None,
                    mask_name="mask.png" if spec.mode in MASK_MODES else None)

    def convert_prompt_with_deepseek(self,user_prompt,mode="txt2img"):
        if not self.provider:return {"success":False,"error":"提示词服务未初始化"}
        try:
            return convert(self.provider,user_prompt,mode,timeout=20)
        except Exception as exc:
            LOG.warning("Prompt conversion failed: %s",type(exc).__name__)
            return {"success":False,"error":"提示词审核或翻译服务暂不可用"}

    def upload_image(self,binary):
        name="anima_"+secrets.token_hex(12)+".png"
        response=requests.post(self.COMFY_URL+"/upload/image",files={"image":(name,binary,"image/png")},data={"type":"input","overwrite":"false"},timeout=30)
        response.raise_for_status();data=response.json()
        filename=data.get("name");folder=data.get("subfolder","")
        if not isinstance(filename,str) or not filename or "/" in filename or "\\" in filename or ".." in folder.replace("\\","/").split("/"):
            raise ValueError("上传响应无效")
        return folder+"/"+filename if folder else filename

    def submit_to_comfyui(self,workflow):
        try:
            q=requests.get(self.COMFY_URL+"/queue",timeout=5);q.raise_for_status();queue=q.json()
            if queue.get("queue_running") or queue.get("queue_pending"):return False,"绘图服务仍有任务，请稍后重试"
            r=requests.post(self.COMFY_URL+"/prompt",json={"prompt":workflow,"client_id":"anima-"+secrets.token_hex(8)},timeout=10)
            r.raise_for_status();data=r.json()
            if not data.get("prompt_id") or data.get("node_errors"):return False,"工作流未通过ComfyUI校验"
            return True,data["prompt_id"]
        except (requests.RequestException,ValueError):return False,"工作流提交失败，请检查节点和模型配置"

    @staticmethod
    def completed_image(history,prompt_id):
        entry=history.get(prompt_id) if isinstance(history,dict) else None
        if not isinstance(entry,dict):return None
        status=entry.get("status",{})
        if status.get("status_str")=="error" or any(isinstance(m,list) and m and m[0] in {"execution_error","execution_interrupted"} for m in status.get("messages",[])):
            raise ValueError("ComfyUI生成失败或被中断")
        if status.get("status_str")!="success" or status.get("completed") is not True:return None
        images=entry.get("outputs",{}).get("47",{}).get("images",[])
        if len(images)!=1 or images[0].get("type")!="output":raise ValueError("本次任务未返回预期图片")
        return images[0]

    def wait_for_generation_polling(self,prompt_id,max_wait=None):
        start=time.monotonic();deadline=start+(self.COMFY_WAIT_TIMEOUT if max_wait is None else max_wait)
        self._last_error=""
        while time.monotonic()<deadline:
            try:
                r=requests.get(self.COMFY_URL+"/history/"+prompt_id,timeout=max(0.1,min(5,deadline-time.monotonic())))
                r.raise_for_status()
                if self.completed_image(r.json(),prompt_id):return True,int(time.monotonic()-start)
            except requests.RequestException:pass
            except (ValueError,TypeError,AttributeError) as exc:
                self._last_error=str(exc);return False,int(time.monotonic()-start)
            time.sleep(max(0,min(1,deadline-time.monotonic())))
        self._last_error="生成等待超时，本次未返回图片；后台可能仍在运行"
        return False,int(time.monotonic()-start)

    def get_image_for_prompt(self,prompt_id):
        r=requests.get(self.COMFY_URL+"/history/"+prompt_id,timeout=5);r.raise_for_status()
        image=self.completed_image(r.json(),prompt_id)
        if not image:raise ValueError("本次任务未完成")
        name=image.get("filename");folder=image.get("subfolder","")
        if not isinstance(name,str) or "/" in name or "\\" in name or ".." in folder.replace("\\","/").split("/"):raise ValueError("输出路径无效")
        body=bytearray()
        # Use the exact prompt_id output, never scan for a latest image.
        mapped = None
        if self.COMFY_OUTPUT_DIR:
            root = Path(self.COMFY_OUTPUT_DIR).resolve()
            candidate = (root / folder / name).resolve()
            if candidate != root and root in candidate.parents:
                mapped = candidate
        if mapped and mapped.is_file():
            if mapped.stat().st_size > 64*1024*1024: raise ValueError("??????")
            body.extend(mapped.read_bytes())
        else:
            with requests.get(self.COMFY_URL+"/view",params={"filename":name,"subfolder":folder,"type":"output"},timeout=30,stream=True) as response:
                response.raise_for_status()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body)>64*1024*1024:raise ValueError("??????")
        with Image.open(io.BytesIO(body)) as output:output.verify()
        self.output_cache.mkdir(parents=True,exist_ok=True)
        destination=self.output_cache/(secrets.token_hex(16)+".png");destination.write_bytes(body)
        return str(destination)

    def delete_image(self,image_path):
        try:
            p=Path(image_path).resolve()
            if p.parent!=self.output_cache.resolve():return False
            p.unlink(missing_ok=True);return True
        except (OSError,ValueError):return False

    def generate(self,user_prompt,*,spec=None,reference=None,mask_png=None):
        start=time.monotonic();spec=spec or Request(user_prompt)
        def fail(error,**extra):return {"success":False,"error":str(error),"elapsed_seconds":int(time.monotonic()-start),**extra}
        try:
            # Dependency validation happens before LLM calls or image uploads.
            self.preflight(spec)
            converted=self.convert_prompt_with_deepseek(user_prompt,spec.mode)
            if not converted.get("success"):
                return fail(converted.get("error","提示词转换失败"),rejected=converted.get("rejected") is True)
            image_name=mask_name=None;size=None
            if spec.mode in IMAGE_MODES:
                if not reference:raise ValueError("当前模式缺少参考图")
                image_name=self.upload_image(reference["png"]);size=(reference["width"],reference["height"])
            if spec.mode in MASK_MODES:
                if not mask_png:raise ValueError("当前模式缺少蒙版")
                mask_name=self.upload_image(mask_png)
            workflow=build_graph(spec,converted["data"],self.anima_config,self.object_info(),image_name,mask_name,size)
            ok,prompt_id=self.submit_to_comfyui(workflow)
            if not ok:return fail(prompt_id)
            ok,_=self.wait_for_generation_polling(prompt_id)
            if not ok:return fail(self._last_error,prompt_id=prompt_id)
            image_path=self.get_image_for_prompt(prompt_id)
            age_group=converted["data"].get("presentation",{}).get("age_group","unknown")
            review=review_image(image_path,age_group=age_group)
            if not review.get("allowed"):
                self.delete_image(image_path)
                return fail(review.get("error","成图未通过安全审核"),output_blocked=True,prompt_id=prompt_id)
            return {"success":True,"image_path":image_path,"prompt_id":prompt_id,"mode":spec.mode,"visual_review":review,
                    "elapsed_seconds":int(time.monotonic()-start)}
        except Exception as exc:
            LOG.warning("Anima mode %s failed: %s",spec.mode,str(exc))
            return fail(str(exc))
