"""Anima prompting: generic request-scoped identities and conservative person defaults."""
import json
import math
import re
from pathlib import Path

from anima.identities import request_hints, exact_tag, normalized
from anima.formatter_adapter import build_system, parse_formatter

QUALITY = "(masterpiece, best quality, amazing quality, very aesthetic, extremely detailed, very detailed, absurdres, newest, highres, score_9, score_8, year 2024, newest,)"
NEGATIVE = ["worst quality", "low quality", "score_1", "score_2", "score_3", "lowres", "blurry", "jpeg artifacts", "censor", "sepia", "shiny skin", "artist name"]
# Unrequested lettering is a stability defect, not a style choice.
TEXT_NEGATIVE = ["text", "watermark", "signature"]
# Default-anime requests drift into monochrome sketch pages without this nudge.
STYLE_NEGATIVE = ["monochrome", "greyscale", "sketch", "lineart"]
LOCAL_POLICY = (Path(__file__).parent / "prompts" / "base_system.txt").read_text(encoding="utf-8")
SYSTEM = build_system(LOCAL_POLICY)
PERSON_RE = re.compile(r"人物|女孩|少女|男孩|少年|角色|女性|男性|人像|肖像|\b(?:girls?|boys?|women|woman|men|man|person|character|portrait)\b",re.I)
STYLE_RE = re.compile(r"写实|真人|照片|摄影|油画|水彩|水墨|版画|像素|雕塑|素描|速写|厚涂|美漫|漫画|卡通|\b(?:3d|realistic|photograph|photo|watercolou?r|oil painting|pixel art|sculpture|sketch|manga|comic|cartoon)\b",re.I)
CLOTHING_RE = re.compile(r"衣|裙|裤|服装|穿着|泳装|泳衣|比基尼|浴衣|旗袍|制服|西装|裸|\b(?:clothes?|clothing|outfit|dress|bikini|swimsuit|yukata|nude)\b",re.I)
POSE_RE = re.compile(r"姿势|回头|站|坐|躺|蹲|跑|跳|抱|亲吻|抬手|挥手|\b(?:pose|standing|sitting|lying|running|jumping|looking back|embrace|kissing|wave)\b",re.I)
COUNT_RE = re.compile(r"\b\d+(?:girls?|boys?|others?)\b",re.I)
TEXT_RE = re.compile(r"文字|字幕|标题|签名|标志|logo|水印|\b(?:text|lettering|signature|watermark|logo|caption|subtitle)\b",re.I)
MONO_RE = re.compile(r"黑白|单色|灰阶|素描|速写|线稿|草图|\b(?:monochrome|greyscale|grayscale|sketch|line ?art|black and white)\b",re.I)
PERSON_TOPIC_RE = re.compile(r"\b(?:yuri|yaoi|girls?[ _-]love|boys?[ _-]love|shou?jo[ _-]ai|shou?nen[ _-]ai)\b", re.I)


def verified_hints(original):
    return request_hints(original)


def literal(value):
    value=str(value).replace(r"\(","(").replace(r"\)",")")
    return value.replace("(",r"\(").replace(")",r"\)")


def clean_list(value,maximum=50):
    if not isinstance(value,list) or len(value)>maximum:
        raise ValueError("Invalid tag list")
    if any(not isinstance(x,str) or len(x)>200 for x in value):
        raise ValueError("Invalid tag value")
    return list(dict.fromkeys(x.strip() for x in value if x.strip()))


def safe_generation(generation):
    if not isinstance(generation,dict):generation={}
    def bounded(key,default,lower,upper,integer=False):
        value=generation.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return default
        if integer and not isinstance(value,int):return default
        return max(lower,min(upper,value))
    return {"steps":bounded("steps",30,30,40,True),"cfg":bounded("cfg",4.3,4.0,5.0),
            "denoise":bounded("denoise",0.45,0.3,0.7),"scale":bounded("scale",2.0,1.0,2.0)}


def person_subject(original,tags,hints,subject_type=None,description=""):
    # Topic tags may imply people even when the literal request is ambiguous.
    return bool((isinstance(subject_type,str) and subject_type in {"person", "people", "human"})
                or hints.get("characters") or PERSON_RE.search(original)
                or PERSON_RE.search(description) or PERSON_TOPIC_RE.search(description)
                or any(PERSON_RE.search(x) or COUNT_RE.search(x) or PERSON_TOPIC_RE.search(x) for x in tags))


NO_EXPOSURE = ["nudity", "nipples", "areola", "exposed genitals", "penis", "vulva", "anus", "explicit sexual activity"]
MINOR_RE = re.compile(r"未成年|儿童|幼女|幼童|萝莉|正太|小学生|初中生|高中生|\b(?:child|children|underage|minor|loli|shota)\b",re.I)
AGE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:岁|[- ]year[- ]old|years? old)",re.I)


def presentation_policy(data,original,person):
    if not person:return {"age_group":"not_applicable","tone":"not_applicable","light_guidance":False}
    explicit_minor=bool(MINOR_RE.search(original) or any(int(age)<18 for age in AGE_RE.findall(original)))
    age=data.get("age_group","unknown")
    if not isinstance(age,str) or age not in {"adult","minor","unknown"}:age="unknown"
    if explicit_minor:age="minor"
    tone=data.get("presentation_tone","unknown")
    if not isinstance(tone,str) or tone not in {"conservative","neutral","romantic","relaxed","unknown"}:tone="unknown"
    light=age!="adult" or tone in {"conservative","neutral","unknown"}
    return {"age_group":age,"tone":tone,"light_guidance":light}


def conservative_defaults(original,person,light_guidance=True):
    # Never impose an outfit or pose in the positive prompt. Exposure/activity
    # limits apply globally; only uncertain/conservative topics get a small nudge.
    negative=list(NO_EXPOSURE)
    if person and light_guidance:
        negative.extend(["unrequested revealing clothing","sexualized pose"])
    return [],negative


def stability_negatives(original,default_anime,artists):
    # Style/lettering stability only; this never substitutes for the factual audit.
    extra=[]
    if default_anime and not artists and not MONO_RE.search(original):
        extra.extend(STYLE_NEGATIVE)
    if not TEXT_RE.search(original):
        extra.extend(TEXT_NEGATIVE)
    return extra


def compile_response(data,original,hints=None):
    if not isinstance(data,dict) or type(data.get("rejected")) is not bool:
        raise ValueError("Missing moderation verdict")
    if data["rejected"]:
        return {"success":False,"rejected":True,"error":str(data.get("reason") or data.get("rejection_reason") or "内容不适合生成")[:300]}
    translation=data.get("english_translation")
    if not isinstance(translation,str) or not translation.strip() or len(translation)>12000:
        raise ValueError("Missing faithful English translation")
    hints=hints or verified_hints(original)
    artists=list(hints["artists"])
    format_source="legacy_or_literal_fallback"
    try:
        formatted=parse_formatter(data)
        if formatted is not None:format_source=formatted["formatter_source"]
        content=formatted if formatted is not None else data
        tags=clean_list(content.get("tags",[]),100);description=content.get("description","")
        if not isinstance(description,str) or len(description)>10000:raise ValueError("Invalid description")
        if data.get("_literal_fallback") is True or (not tags and not description.strip()):
            raise ValueError("Use original request and faithful translation fallback")
        raw_artists=data.get("artists",[])
        if not isinstance(raw_artists,list) or len(raw_artists)>3:raise ValueError("Invalid artists")
        for artist in raw_artists:
            if isinstance(artist,str):name=source=artist.lstrip("@")
            elif isinstance(artist,dict):name,source=artist.get("name",""),artist.get("source_text","")
            else:raise ValueError("Invalid artist")
            if (isinstance(name,str) and isinstance(source,str) and source.strip()
                    and source.casefold() in original.casefold() and len(name)<120):
                canonical=exact_tag(name,1)
                if canonical:
                    artists=[x for x in artists if normalized(x)!=normalized(source)]
                    if normalized(canonical) not in artists:artists.append(normalized(canonical))
        # Verified canonical names only seed a literal fallback; factual prose is
        # established upstream by retrieval + semantic audit, never word filters.
        if formatted is None and not tags and hints.get("characters"):
            for identity in hints["characters"]:
                tags.append(identity["canonical_tag"])
                if identity.get("series_tag"):tags.append(identity["series_tag"])
        tags=[literal(x.lower().replace("_"," ")) for x in tags
              if not x.startswith("@") and normalized(x) not in set(artists)]
        person=person_subject(original,tags,hints,data.get("subject_type"),description)
        default_anime = person and not STYLE_RE.search(original)
        if default_anime:
            tags.append("anime illustration")
            if not artists:
                tags.extend(["anime coloring", "2d illustration"])
        policy=presentation_policy(data,original,person)
        conservative,exclusions=conservative_defaults(original,person,policy["light_guidance"])
        if default_anime and not artists:exclusions.extend(["photorealistic", "3d", "realistic"])
        exclusions.extend(stability_negatives(original,default_anime,artists))
        tags=list(dict.fromkeys(tags+conservative))
        parts=[QUALITY]+tags+["@"+literal(x) for x in artists]
        if tags or description.strip() or artists:
            positive=", ".join(parts)
            if description.strip():positive+="\n\n"+literal(description.strip())
        else:positive=QUALITY+". "+literal(original)+". "+literal(translation.strip())
        extras=[literal(x) for x in clean_list(data.get("negative_tags",[]),20) if x.casefold() in original.casefold()]
        negative=", ".join(dict.fromkeys(NEGATIVE+exclusions+extras))
    except (ValueError,TypeError):
        format_source="literal_fallback"
        positive=QUALITY+". "+literal(original)+". "+literal(translation.strip())
        artists=list(hints["artists"])
        for identity in hints.get("characters",[]):
            positive+=", "+literal(identity["canonical_tag"])
            if identity.get("series_tag"):positive+=", "+identity["series_tag"]
        person=person_subject(original,[],hints,data.get("subject_type"),translation)
        default_anime = person and not STYLE_RE.search(original)
        if default_anime:
            positive+=", anime illustration"
            if not artists:positive+=", anime coloring, 2d illustration"
        policy=presentation_policy(data,original,person)
        conservative,exclusions=conservative_defaults(original,person,policy["light_guidance"])
        if default_anime and not artists:exclusions.extend(["photorealistic", "3d", "realistic"])
        exclusions.extend(stability_negatives(original,default_anime,artists))
        for x in conservative:positive+=", "+x
        for x in artists:positive+=", @"+literal(x)
        negative=", ".join(NEGATIVE+exclusions)
    aspect=data.get("aspect")
    if aspect not in {None,"portrait","landscape","square"}:aspect=None
    return {"success":True,"data":{"positive_prompt":positive,"negative_prompt":negative,
            "artists":artists,"aspect":aspect,"presentation":policy,
            "formatter_source":format_source,"generation":safe_generation(data.get("generation",{}))}}


def convert(provider,original,mode,timeout=20):
    from anima.grounding import run_grounded, parse_stage_json
    hints=request_hints(original,allow_lookup=True)
    try:
        data,trace=run_grounded(provider,original,mode,hints,LOCAL_POLICY,SYSTEM,timeout)
        result=compile_response(data,original,hints=hints)
        if result.get("success"):result["data"]["grounding"]=trace
        return result
    except (ValueError,TypeError,KeyError):
        # Bounded recovery still independently moderates and translates the
        # original request; never claim failed retrieval was verified.
        messages=[{"role":"system","content":LOCAL_POLICY+"\nRECOVERY STAGE OVERRIDES ALL OUTPUT EXAMPLES. Return ONLY a strict JSON object: rejected=true and reason, OR rejected=false and english_translation with a faithful, complete translation. Preserve ambiguity and every explicit age, color and action. Never emit tags, formatter, generation, artists, descriptions or new appearance details."},
                  {"role":"user","content":json.dumps({"request":original,"mode":mode,"verified_hints":hints},ensure_ascii=False)}]
        raw=provider.send(messages,prompt_cache_key="anima-grounded-literal-recovery-v1",timeout=timeout)
        recovery=parse_stage_json(raw)
        # Stage ownership, not a hallucinated-word blacklist: recovery can only
        # authorize/translate. It cannot smuggle a new creative assembly.
        data={**{key:recovery[key] for key in ("rejected","reason","english_translation") if key in recovery},"_literal_fallback":True}
        result=compile_response(data,original,hints=hints)
        if result.get("success"):result["data"]["grounding"]={"stage":"literal_recovery","tools":[]}
        return result
