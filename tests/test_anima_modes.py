"""Offline contracts for Yuri-derived commands, media, graphs and follow-ups."""
import io
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from PIL import Image

from anima.commands import Request, parse_request, IMAGE_MODES
from anima.media import prepare_reference, make_mask
from anima.workflows import build_graph
from anima.prompting import compile_response, convert
from anima.anima_service import AnimaService


def image_bytes(size=(64,64), color='red', fmt='PNG'):
    out=io.BytesIO(); Image.new('RGB',size,color).save(out,format=fmt);return out.getvalue()


def schema():
    kinds=['UNETLoader','CLIPLoader','VAELoader','CLIPTextEncode','EmptyLatentImage','KSampler','VAEDecode','SaveImage','LoadImage','VAEEncode','VAEEncodeForInpaint','ImageToMask','ImageCompositeMasked','ImageScaleBy','ImageScale','ImageUpscaleWithModel']
    result={k:{'input':{'required':{}}} for k in kinds}
    for kind,field,value in [('UNETLoader','unet_name','anima-base-v1.0.safetensors'),('CLIPLoader','clip_name','qwen_3_06b_base.safetensors'),('VAELoader','vae_name','qwen_image_vae.safetensors'),('UpscaleModelLoader','model_name','RealESRGAN_x4plus_anime_6B.pth')]:
        result[kind]={'input':{'required':{field:([value],)}}}
    result['AnimaLLLiteApply_sdscripts']={'input':{'required':{'lllite_name':(['anima-lllite-any-test-like-v2.safetensors','anima-lllite-inpainting-v1.safetensors'],)}}}
    for kind in ['AnimaArtistPack','AnimaArtistAdapterMixer']:
        result[kind]={'input':{'required':{}}}
    return result


def prompt():
    return {'positive_prompt':'masterpiece, best quality, @wlop, @anmi, 1girl','negative_prompt':'blurry','artists':['wlop','anmi'],'aspect':None}


@pytest.mark.parametrize('name,mode',[('文生图','txt2img'),('图生图','img2img'),('画师混合','artist_mix'),('放大','resize'),('超分','upscale')])
def test_modes(name,mode):
    assert parse_request('用户提示词\n'+name).mode==mode
    assert parse_request('用户提示词\r\n/'+name).mode==mode


def test_default_and_selector_only():
    assert parse_request('女孩在花园').mode=='txt2img'
    req = parse_request('第一行\n未知说明 CFG=100\n图生图 强度=0.9 种子=42')
    assert req.prompt == '第一行' and req.mode == 'img2img' and req.options == {}
    assert parse_request('帮助') is None
    req = parse_request('女孩\n未知 步数=999')
    assert req.mode == 'txt2img' and req.prompt == '女孩' and req.options == {}



@pytest.mark.parametrize('name', ['局部修正','局部重绘','色彩参考','控制重绘','inpaint','color','control-inpaint'])
def test_disabled_modes_are_not_executed(name):
    with pytest.raises(ValueError, match='停用'):
        parse_request('画人物\n'+name)

@pytest.mark.parametrize('mode',['txt2img','img2img','artist_mix','resize','upscale'])
def test_active_graphs_have_valid_links(mode):
    graph=build_graph(Request('draw',mode),prompt(),{},schema(),'ref.png','mask.png',(768,768))
    for item in graph.values():
        for link in item['inputs'].values():
            if isinstance(link,list): assert str(link[0]) in graph
    assert graph['47']['class_type']=='SaveImage'


def test_llm_parameters_are_bounded_and_user_parameters_ignored():
    data={'rejected':False,'english_translation':'girl','tags':['1girl'],
          'generation':{'steps':100,'cfg':100,'denoise':0.05,'scale':5},'aspect':'portrait'}
    result=compile_response(data,'女孩')['data']
    assert result['generation']=={'steps':40,'cfg':5.0,'denoise':0.3,'scale':2.0}
    graph=build_graph(Request('girl','txt2img',{'steps':8,'cfg':1,'seed':123}),result,{},schema())
    assert graph['19']['inputs']['steps']==40 and graph['19']['inputs']['cfg']==5
    assert graph['28']['inputs']['width']==832 and graph['28']['inputs']['height']==1216
    assert graph['19']['inputs']['seed'] != 123


def test_invalid_llm_parameters_use_defaults():
    from anima.prompting import safe_generation
    assert safe_generation({'steps':True,'cfg':float('nan'),'scale':'4','denoise':None}) == {
        'steps':30,'cfg':4.3,'denoise':0.45,'scale':2.0}


def test_mapped_output_uses_exact_job(tmp_path):
    service=AnimaService.__new__(AnimaService);service.COMFY_URL='http://test'
    service.COMFY_OUTPUT_DIR=str(tmp_path/'mapped');service.output_cache=tmp_path/'cache'
    out=tmp_path/'mapped'/'Anima'/'job';out.mkdir(parents=True)
    wanted=image_bytes(color='red');(out/'wanted.png').write_bytes(wanted)
    (out/'other.png').write_bytes(image_bytes(color='blue'))
    job={'job':{'status':{'status_str':'success','completed':True,'messages':[]},
        'outputs':{'47':{'images':[{'filename':'wanted.png','subfolder':'Anima/job','type':'output'}]}}}}
    reply=Mock();reply.json.return_value=job
    with patch('anima.anima_service.requests.get',return_value=reply) as get:
        result=service.get_image_for_prompt('job')
    assert Path(result).read_bytes()==wanted and get.call_count==1 and (out/'wanted.png').exists()


def test_legal_abstract_fallback_and_rejection():
    out=compile_response({'rejected':False,'english_translation':'The shape of silence'},'沉默的形状')
    assert '沉默的形状' in out['data']['positive_prompt']
    assert 'The shape of silence' in out['data']['positive_prompt']
    assert compile_response({'rejected':True,'reason':'不合规'},'test')['rejected']


def test_stale_followup_options_are_discarded():
    value=Request.from_payload({'prompt':'girl','mode':'img2img','options':{'seed':1,'steps':1}})
    assert value.options == {}
    with pytest.raises(ValueError):Request.from_payload({'prompt':'girl','mode':'inpaint'})


def test_namie_character_and_style_survive_missing_llm_fields():
    result=compile_response({'rejected':False,'english_translation':'Goldenglow','tags':['1girl','goldenglow'],'artists':[]},'@Namie \u6f84\u95ea')
    value=result['data']['positive_prompt']
    assert 'goldenglow' in value
    assert '@namie' in value and 'anime illustration' in value
    assert '@namie-kun' not in value


def test_explicit_medium_not_replaced_by_default_anime():
    result=compile_response({'rejected':False,'english_translation':'a woman in watercolor','tags':['1girl'],'artists':[]},'\u6c34\u5f69\u5973\u5b69')
    assert 'anime illustration' not in result['data']['positive_prompt']


def test_optional_search_is_entity_only_and_preserves_moderation():
    first={'rejected':False,'english_translation':'Saria','search_request':{'kind':'character','source_text':'Saria'}}
    draft={'rejected':False,'english_translation':'Saria','formatter':{'xml_out':'saria, arknights\n\nSaria is the main subject.','text_out':''}}
    provider=Mock();provider.send.side_effect=[json.dumps(first),json.dumps(draft),json.dumps({'supported':True})]
    with patch('anima.grounding.gather_evidence',return_value=[]), patch('anima.search.lookup_entity',return_value=[{'title':'Saria','url':'https://example.org','snippet':'Saria Arknights'}]) as lookup:
        result=convert(provider,'Saria','txt2img')
    assert result['success'] and 'arknights' in result['data']['positive_prompt']
    lookup.assert_called_once_with('Saria','character')

def test_search_does_not_forward_unrelated_or_untrusted_query():
    first={'rejected':False,'english_translation':'Saria','search_request':{'kind':'character','source_text':'private data'}}
    draft={'rejected':False,'english_translation':'Saria','formatter':{'xml_out':'saria\n\nSaria is the main subject.','text_out':''}}
    provider=Mock();provider.send.side_effect=[json.dumps(first),json.dumps(draft),json.dumps({'supported':True})]
    with patch('anima.grounding.gather_evidence',return_value=[]), patch('anima.search.lookup_entity') as lookup:
        assert convert(provider,'Saria','txt2img')['success']
    lookup.assert_not_called()

def test_system_never_contains_other_character_example():
    from anima.prompting import SYSTEM, verified_hints
    assert 'goldenglow' not in SYSTEM.lower()
    assert verified_hints('@Anmi \u827e\u96c5\u6cd5\u62c9')['characters'] == []


def test_person_defaults_modest_but_explicit_yukata_preserved():
    data={'rejected':False,'english_translation':'girl','tags':['1girl'],'artists':[]}
    value=compile_response(data,'\u5973\u5b69')['data']['positive_prompt']
    assert 'modest clothing' not in value and 'neutral pose' not in value
    value=compile_response(data,'\u5973\u5b69\u7a7f\u6d74\u8863')['data']['positive_prompt']
    assert 'modest clothing' not in value and 'neutral pose' not in value


def test_exact_current_identity_is_checked_with_semantic_audit_not_clipping():
    from anima.grounding import run_grounded
    evidence={'artists':['anmi'],'characters':[{'source_text':'艾雅法拉','canonical_tag':'eyjafjalla (arknights)','series_tag':'arknights'}],'identity_only':True}
    plan={'rejected':False,'english_translation':'Eyjafjalla'}
    bad={'rejected':False,'english_translation':'Eyjafjalla','formatter':{'xml_out':'goldenglow\n\nA different character appears.','text_out':''}}
    corrected={'xml_out':'eyjafjalla (arknights), arknights\n\nEyjafjalla is the main subject.','text_out':''}
    provider=Mock();provider.send.side_effect=[json.dumps(plan),json.dumps(bad),json.dumps({'supported':False,'issues':[{'quote':'goldenglow','problem':'wrong identity'}],'formatter':corrected}),json.dumps({'supported':True})]
    with patch('anima.grounding.gather_evidence',return_value=[]):
        raw,trace=run_grounded(provider,'@Anmi 艾雅法拉','txt2img',evidence,'policy','format',20)
    value=compile_response(raw,'@Anmi 艾雅法拉',hints=evidence)['data']['positive_prompt']
    assert 'eyjafjalla' in value and 'goldenglow' not in value
    assert trace['stage']=='semantically_revised' and provider.send.call_count==4

def test_no_person_defaults_for_food_and_flowers():
    for original,tags in [('香酥鸡锁骨',['food','fried chicken']),('百合花',['lily','flower'])]:
        value=compile_response({'rejected':False,'english_translation':'food or flowers','tags':tags},original)['data']
        assert 'modest clothing' not in value['positive_prompt']
        assert 'anime illustration' not in value['positive_prompt']
        assert 'underwear' not in value['negative_prompt']


def test_pose_and_clothing_safeguards_are_independent():
    raw={'rejected':False,'english_translation':'a girl','tags':['1girl']}
    value=compile_response(raw,'女孩回头微笑')['data']
    assert 'modest clothing' not in value['positive_prompt'] and 'neutral pose' not in value['positive_prompt']
    value=compile_response(raw,'女孩穿浴衣')['data']
    assert 'modest clothing' not in value['positive_prompt'] and 'neutral pose' not in value['positive_prompt']
    value=compile_response(raw,'女孩穿浴衣回头微笑')['data']
    assert 'modest clothing' not in value['positive_prompt'] and 'neutral pose' not in value['positive_prompt']


def test_public_identity_requires_exact_alias_and_known_character_category():
    from anima.identities import public_character
    public_character.cache_clear()
    reply=Mock();reply.json.return_value=[{'id':999,'title':'eyjafjalla_(arknights)','other_names':['艾雅法拉']}]
    with patch('anima.identities.requests.get',return_value=reply) as get:
        out=public_character('艾雅法拉')
    assert out['canonical_tag']=='eyjafjalla (arknights)' and out['series_tag']=='arknights'
    assert get.call_args.kwargs['params']['search[other_names_match]']=='艾雅法拉'
    public_character.cache_clear()
    with patch('anima.identities.requests.get',return_value=reply):
        assert public_character('不同角色') is None
    public_character.cache_clear()


def test_plain_artist_only_request_retains_valid_tag():
    result=compile_response({'rejected':False,'english_translation':'Anmi style','tags':[],'artists':[]},'@Anmi')['data']
    assert '@anmi' in result['positive_prompt']


def test_fallback_still_has_character_clothing_and_pose_protection():
    evidence={'artists':['anmi'],'characters':[{'canonical_tag':'eyjafjalla (arknights)','series_tag':'arknights'}],'identity_only':True}
    raw={'rejected':False,'english_translation':'a character','tags':'malformed'}
    value=compile_response(raw,'@Anmi 艾雅法拉',hints=evidence)['data']
    assert 'eyjafjalla' in value['positive_prompt'] and 'modest clothing' not in value['positive_prompt']
    assert 'nipples' in value['negative_prompt']


@pytest.mark.parametrize('topic',['yuri','girls love','yaoi','boys love','shoujo ai'])
def test_implicit_person_topics_get_anime_and_modesty(topic):
    raw={'rejected':False,'english_translation':topic,'tags':[topic]}
    result=compile_response(raw,'题材')['data']
    for value in ['anime illustration','anime coloring','2d illustration']:
        assert value in result['positive_prompt']
    assert 'nudity' in result['negative_prompt'] and 'photorealistic' in result['negative_prompt']


def test_semantic_person_and_fallback_receive_same_style():
    raw={'rejected':False,'english_translation':'a couple','tags':[], 'subject_type':'person'}
    result=compile_response(raw,'场景')['data']
    assert 'anime coloring' in result['positive_prompt']
    raw['tags']='invalid'
    result=compile_response(raw,'场景')['data']
    assert 'anime coloring' in result['positive_prompt'] and 'modest clothing' not in result['positive_prompt']
    assert 'unrequested revealing clothing' in result['negative_prompt']


def test_flower_meaning_of_lily_not_changed_into_person():
    raw={'rejected':False,'english_translation':'lily flowers','tags':['lily','flower'],'subject_type':'object'}
    result=compile_response(raw,'百合花')['data']
    assert 'anime coloring' not in result['positive_prompt'] and 'modest clothing' not in result['positive_prompt']


def test_explicit_medium_and_artist_override_anime_base():
    raw={'rejected':False,'english_translation':'two women','tags':['yuri'],'subject_type':'person'}
    result=compile_response(raw,'水彩风格')['data']
    assert 'anime coloring' not in result['positive_prompt']
    assert 'photorealistic' not in result['negative_prompt']
    result=compile_response(raw,'@Anmi')['data']
    assert '@anmi' in result['positive_prompt'] and 'anime coloring' not in result['positive_prompt']


def test_adult_romantic_topics_have_no_fixed_clothing_or_pose():
    raw={'rejected':False,'english_translation':'two adult women in love','tags':['yuri'],
         'subject_type':'person','age_group':'adult','presentation_tone':'romantic'}
    result=compile_response(raw,'成人恋爱题材')['data']
    assert result['presentation']['light_guidance'] is False
    for term in ['modest clothing','neutral pose','fully clothed']:
        assert term not in result['positive_prompt']
    for term in ['underwear','sexualized pose','unrequested revealing clothing']:
        assert term not in result['negative_prompt']
    assert 'nipples' in result['negative_prompt'] and 'exposed genitals' in result['negative_prompt']


@pytest.mark.parametrize('tone',['unknown','neutral','conservative'])
def test_conservative_or_uncertain_topics_get_only_light_negative_guidance(tone):
    raw={'rejected':False,'english_translation':'adult person','tags':['1girl'],
         'subject_type':'person','age_group':'adult','presentation_tone':tone}
    result=compile_response(raw,'普通人物')['data']
    assert result['presentation']['light_guidance']
    assert 'unrequested revealing clothing' in result['negative_prompt']
    assert 'sexualized pose' in result['negative_prompt']
    assert 'modest clothing' not in result['positive_prompt'] and 'neutral pose' not in result['positive_prompt']


def test_explicit_minor_cannot_be_relaxed_by_llm_age():
    raw={'rejected':False,'english_translation':'a 14-year-old girl in yukata','tags':['1girl','yukata'],
         'subject_type':'person','age_group':'adult','presentation_tone':'relaxed'}
    result=compile_response(raw,'14岁少女穿浴衣')['data']
    assert result['presentation']['age_group']=='minor' and result['presentation']['light_guidance']


def review_flags(**changes):
    flags={'image_readable':True,'extreme_gore':False,'disturbing_horror':False,'cthulhu_horror':False,
           'exposed_nipples':False,'exposed_genitals':False,'explicit_sexual_activity':False,'sexualized_minor':False}
    flags.update(changes)
    return flags


def test_visual_review_allows_age_uncertain_nonexplicit_content():
    from anima.safety import validate_review
    assert validate_review(review_flags(uncertain=True,suggestive_presentation=True),'unknown')['allowed']
    assert validate_review(review_flags(),'minor')['allowed']


@pytest.mark.parametrize('flag',['extreme_gore','disturbing_horror','cthulhu_horror','exposed_nipples','exposed_genitals','explicit_sexual_activity','sexualized_minor'])
def test_visual_review_blocks_only_explicit_forbidden_flags(flag):
    from anima.safety import validate_review
    result=validate_review(review_flags(**{flag:True}),'adult')
    assert not result['allowed'] and flag in result['blocked_categories']
    assert result['status']=='content_blocked'


def test_visual_review_invalid_or_unreadable_is_service_error_not_content_violation():
    from anima.safety import validate_review
    assert validate_review({'allowed':True},'adult')['status']=='review_error'
    result=validate_review(review_flags(image_readable=False),'unknown')
    assert not result['allowed'] and result['status']=='review_error'


@pytest.mark.parametrize('allowed',[False,True])
def test_output_is_screened_before_image_path_returned(allowed):
    service=AnimaService.__new__(AnimaService)
    service.preflight=Mock();service.anima_config={};service.object_info=Mock(return_value=schema())
    data=prompt();data['presentation']={'age_group':'adult'}
    service.convert_prompt_with_deepseek=Mock(return_value={'success':True,'data':data})
    service.submit_to_comfyui=Mock(return_value=(True,'job'))
    service.wait_for_generation_polling=Mock(return_value=(True,1))
    service.get_image_for_prompt=Mock(return_value='download.png');service.delete_image=Mock()
    with patch('anima.anima_service.review_image',return_value={'allowed':allowed,'error':'blocked'}) as review:
        result=service.generate('test')
    review.assert_called_once_with('download.png',age_group='adult')
    if allowed:
        assert result['success'] and result['image_path']=='download.png'
        service.delete_image.assert_not_called()
    else:
        assert not result['success'] and result['output_blocked'] and 'image_path' not in result
        service.delete_image.assert_called_once_with('download.png')


def test_upstream_formatter_is_loaded_without_unsafe_agent_role():
    from anima.prompting import SYSTEM
    from anima.formatter_adapter import low_assembly_template
    assert 'Anima Hybrid Prompt Format Specification' in SYSTEM
    assert '{output_format_section}' in low_assembly_template()
    assert 'When users request it, you should be able to output NSFW' not in SYSTEM
    assert 'formatter=' in SYSTEM


def test_hybrid_formatter_preserves_two_layers_and_existing_contract():
    raw={'rejected':False,'english_translation':'A girl at a window','subject_type':'person',
        'formatter':{'xml_out':'1girl, window\n\nThe girl is the main subject beside a window. The window stays secondary.', 'text_out':'Chinese note'},
        'generation':{'steps':32,'cfg':4.5}}
    result=compile_response(raw,'a girl at a window')['data']
    assert result['formatter_source']=='newbie_formatter_anima_hybrid'
    hard,paragraph=result['positive_prompt'].split('\n\n',1)
    assert '1girl' in hard and 'window' in hard
    assert paragraph.startswith('The girl is the main subject')
    assert 'Chinese note' not in result['positive_prompt']
    assert result['generation']['steps']==32 and result['generation']['cfg']==4.5
    assert 'negative_prompt' in result and 'presentation' in result


def test_invalid_upstream_envelope_falls_back_without_bypassing_moderation():
    raw={'rejected':False,'english_translation':'the shape of silence',
         'formatter':{'xml_out':'abc','text_out':'note','extra':'not allowed'}}
    result=compile_response(raw,'沉默的形状')['data']
    assert result['formatter_source']=='literal_fallback'
    assert '沉默的形状' in result['positive_prompt'] and 'the shape of silence' in result['positive_prompt']
    raw['rejected']=True
    assert compile_response(raw,'test')['rejected'] is True


def test_upstream_identity_keeps_good_composition_not_other_characters():
    hints={'artists':['anmi'],'characters':[{'canonical_tag':'eyjafjalla (arknights)','series_tag':'arknights'}],'identity_only':True}
    raw={'rejected':False,'english_translation':'Eyjafjalla',
        'formatter':{'xml_out':'1girl, eyjafjalla (arknights), arknights, @anmi\n\nEyjafjalla is the primary subject, with the background remaining secondary. Her silhouette is clearly readable.', 'text_out':''}}
    value=compile_response(raw,'@Anmi 艾雅法拉',hints=hints)['data']['positive_prompt']
    assert 'eyjafjalla' in value and '@anmi' in value
    assert '\n\nEyjafjalla is the primary subject' in value


def test_upstream_format_preserves_explicit_minor_and_all_visual_constraints():
    raw={'rejected':False,'english_translation':'a 14-year-old girl in yukata',
        'age_group':'adult','subject_type':'person',
        'formatter':{'xml_out':'1girl, pink hair, long hair, heterochromia, blue eye, yellow eye, yukata, looking back, fireworks\n\nA 14-year-old girl in a yukata looks back with a slightly open mouth and a smile. The night seaside fireworks remain visible behind her.', 'text_out':''}}
    result=compile_response(raw,'14岁少女穿浴衣回头微笑')['data']
    assert result['presentation']['age_group']=='minor'
    assert '14-year-old' in result['positive_prompt'] and 'slightly open mouth' in result['positive_prompt']
    assert 'heterochromia' in result['positive_prompt']


def test_supported_appearance_sentences_are_not_removed_by_word_filter():
    raw={'rejected':False,'english_translation':'pink-haired girl with blue eyes','formatter':{
         'xml_out':'1girl, pink hair, blue eyes\n\nThe girl has pink hair and blue eyes. Her silhouette is readable.','text_out':''}}
    value=compile_response(raw,'粉发蓝眼女孩')['data']['positive_prompt']
    assert 'pink hair and blue eyes' in value
    import anima.formatter_adapter as adapter
    assert not hasattr(adapter,'guard_identity_paragraph')

def test_normal_minor_depiction_remains_permitted_but_not_aged_up():
    raw={'rejected':False,'english_translation':'a 14-year-old girl wearing yukata at a festival',
         'tags':['1girl','yukata'],'age_group':'minor','subject_type':'person'}
    value=compile_response(raw,'14岁少女穿浴衣在节庆中')['data']
    assert value['presentation']['age_group']=='minor'
    from anima.prompting import LOCAL_POLICY
    assert '不能仅因出现年龄小于18岁就拒绝' in LOCAL_POLICY
