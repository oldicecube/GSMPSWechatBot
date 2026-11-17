"""API-mode branches extracted from the supplied Yuri AIO workflow."""
import secrets

from anima.commands import ensure_enabled

SOURCE_NODES = {"sampler":118, "txt_latent":1428, "img_latent":1426, "mask_latent":1424,
                "color_control":1038, "mask_control":1039, "artist_pack":1690, "artist_mixer":1691}
DEPENDENCIES = {
    "color": "安装 kohya-ss/ComfyUI-Anima-LLLite，并配置 controlnet 中的 any-test-like 权重",
    "control_inpaint": "安装 kohya-ss/ComfyUI-Anima-LLLite，并配置 controlnet 中的 inpainting 权重",
    "artist_mix": "安装 An1X3R/Anima-Artist-Mixer，重启后需有 AnimaArtistPack/AnimaArtistAdapterMixer 节点",
    "upscale": "安装配置指定的放大模型到 models/upscale_models",
}


def node(kind, **inputs):
    return {"class_type":kind, "inputs":inputs}


def custom_node(kind, schema, **overrides):
    if kind not in schema:
        raise ValueError("ComfyUI 缺少节点 " + kind)
    required = schema[kind].get("input", {}).get("required", {})
    inputs = dict(overrides)
    for key, spec in required.items():
        if key in inputs: continue
        options = spec[1] if len(spec)>1 and isinstance(spec[1],dict) else {}
        if "default" in options: inputs[key] = options["default"]
        elif isinstance(spec[0],list) and spec[0]: inputs[key] = spec[0][0]
        elif spec[0] == "COMBO" and options.get("options"): inputs[key] = options["options"][0]
        else: raise ValueError("自定义节点接口已变化，缺少参数：" + kind + "." + key)
    return node(kind, **inputs)


def require_model(schema, kind, field, name):
    info = schema.get(kind)
    if not info: raise ValueError("ComfyUI 缺少节点 " + kind)
    spec = info.get("input",{}).get("required",{}).get(field) or info.get("input",{}).get("optional",{}).get(field)
    choices = None
    if spec and isinstance(spec[0],list):
        choices = spec[0]
    elif spec and spec[0] == "COMBO" and len(spec)>1:
        choices = spec[1].get("options")
    if choices is not None and name not in choices:
        raise ValueError("模型列表中没有 " + name + "，请配置正确的文件名并刷新ComfyUI")


def build_graph(spec, prompt, config, schema, image_name=None, mask_name=None, size=None):
    ensure_enabled(spec.mode)
    opts, mode = prompt.get("generation", {}), spec.mode
    # Never trust serialized/user-supplied execution parameters.
    from anima.prompting import safe_generation
    opts = safe_generation(opts)
    width,height = size or {"portrait":(832,1216),"landscape":(1216,832)}.get(prompt.get("aspect"),(1024,1024))
    graph = {
        "44":node("UNETLoader",unet_name=config.get("model","anima-base-v1.0.safetensors"),weight_dtype="default"),
        "45":node("CLIPLoader",clip_name=config.get("clip","qwen_3_06b_base.safetensors"),type="stable_diffusion",device="default"),
        "15":node("VAELoader",vae_name=config.get("vae","qwen_image_vae.safetensors")),
        "11":node("CLIPTextEncode",clip=["45",0],text=prompt["positive_prompt"]),
        "12":node("CLIPTextEncode",clip=["45",0],text=prompt["negative_prompt"]),
        "28":node("EmptyLatentImage",width=width,height=height,batch_size=1),
        "19":node("KSampler",model=["44",0],positive=["11",0],negative=["12",0],latent_image=["28",0],
                  seed=secrets.randbits(63),steps=opts["steps"],cfg=opts["cfg"],
                  sampler_name="euler",scheduler="simple",denoise=1.0),
        "8":node("VAEDecode",samples=["19",0],vae=["15",0]),
        "47":node("SaveImage",images=["8",0],filename_prefix="Anima/"+secrets.token_hex(12)),
    }
    if image_name:
        graph["50"]=node("LoadImage",image=image_name)
    if mask_name:
        graph["51"]=node("LoadImage",image=mask_name)
        graph["52"]=node("ImageToMask",image=["51",0],channel="red")
    if mode in {"img2img","inpaint"}:
        if not image_name: raise ValueError("此模式需要参考图")
        if mode == "img2img":
            graph["28"]=node("VAEEncode",pixels=["50",0],vae=["15",0])
            graph["19"]["inputs"]["denoise"]=opts.get("denoise",0.45)
        else:
            if not mask_name: raise ValueError("局部修正需要蒙版")
            graph["28"]=node("VAEEncodeForInpaint",pixels=["50",0],vae=["15",0],mask=["52",0],grow_mask_by=22)
            graph["19"]["inputs"]["denoise"]=opts.get("denoise",0.8)
    if mode in {"color","control_inpaint"}:
        if not image_name: raise ValueError("此模式需要参考图")
        field = "color_controlnet" if mode=="color" else "inpaint_controlnet"
        default = "anima-lllite-any-test-like-v2.safetensors" if mode=="color" else "anima-lllite-inpainting-v1.safetensors"
        weights=config.get(field,default)
        require_model(schema,"AnimaLLLiteApply_sdscripts","lllite_name",weights)
        inputs=dict(model=["44",0],image=["50",0],lllite_name=weights,strength=opts.get("strength",0.68 if mode=="color" else 1.0),start_percent=0.0,end_percent=1.0,preserve_wrapper=True)
        if mode=="control_inpaint":
            if not mask_name: raise ValueError("控制重绘需要蒙版")
            inputs["mask"]=["52",0]
        graph["60"]=custom_node("AnimaLLLiteApply_sdscripts",schema,**inputs)
        graph["19"]["inputs"]["model"]=["60",0]
    if mode in {"inpaint","control_inpaint"}:
        # Force the unmasked region to come from the reference after VAE decoding.
        graph["70"]=node("ImageCompositeMasked",destination=["50",0],source=["8",0],x=0,y=0,resize_source=False,mask=["52",0])
        graph["47"]["inputs"]["images"]=["70",0]
    if mode=="artist_mix":
        artists=prompt.get("artists",[])
        if len(artists)<2: raise ValueError("画师混合需在提示词中明确写出至少两位画师；不会自动添加画师")
        base=prompt["positive_prompt"]
        for artist in artists: base=base.replace("@"+artist,"")
        graph["61"]=custom_node("AnimaArtistPack",schema,clip=["45",0],artist_chain=", ".join(artists),base_prompt=base)
        graph["62"]=custom_node("AnimaArtistAdapterMixer",schema,model=["44",0],artist_pack=["61",0],strength=1.0,normalize_weights=True,alignment_mode="base_anchored",enabled=True,apply_to_uncond=False,uncond_strength=0.0)
        graph["19"]["inputs"].update(model=["62",0],positive=["62",1])
    if mode in {"resize","upscale"}:
        if not image_name: raise ValueError("此模式需要参考图")
        if mode=="resize":
            graph["71"]=node("ImageScaleBy",image=["50",0],upscale_method="lanczos",scale_by=opts.get("scale",2.0))
        else:
            name=config.get("upscale_model","RealESRGAN_x4plus_anime_6B.pth")
            require_model(schema,"UpscaleModelLoader","model_name",name)
            graph["72"]=node("UpscaleModelLoader",model_name=name)
            graph["73"]=node("ImageUpscaleWithModel",upscale_model=["72",0],image=["50",0])
            graph["71"]=node("ImageScale",image=["73",0],upscale_method="lanczos",width=round(width*opts.get("scale",2)),height=round(height*opts.get("scale",2)),crop="disabled")
        graph["47"]["inputs"]["images"]=["71",0]
    # Retain only output ancestors, so resize never loads diffusion/text weights.
    keep=set()
    def visit(n):
        if n in keep:return
        keep.add(n)
        for value in graph[n]["inputs"].values():
            if isinstance(value,list) and len(value)==2 and str(value[0]) in graph:
                visit(str(value[0]))
    visit("47")
    graph={key:value for key,value in graph.items() if key in keep}
    for ident,item in graph.items():
        kind=item["class_type"]
        if kind not in schema: raise ValueError("ComfyUI缺少节点："+kind+"。"+DEPENDENCIES.get(mode,"请更新核心节点"))
        required=schema[kind].get("input",{}).get("required",{})
        for name in required:
            if name not in item["inputs"]: raise ValueError("节点版本不兼容："+kind+"."+name)
    for kind,field,filename in [("UNETLoader","unet_name",config.get("model","anima-base-v1.0.safetensors")),("CLIPLoader","clip_name",config.get("clip","qwen_3_06b_base.safetensors")),("VAELoader","vae_name",config.get("vae","qwen_image_vae.safetensors"))]:
        if any(item["class_type"]==kind for item in graph.values()): require_model(schema,kind,field,filename)
    return graph
