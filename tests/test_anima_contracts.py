"""Stage ownership, strict contracts and semantic repair regressions."""
import json
from unittest.mock import Mock, patch
import pytest
from anima.grounding import run_grounded, parse_stage_json
from anima.prompting import convert, compile_response, SYSTEM

HINTS={'artists':[],'characters':[]}

def run(replies, original='a pink-haired girl around 14 years old'):
    provider=Mock();provider.send.side_effect=[json.dumps(x) if isinstance(x,dict) else x for x in replies]
    with patch('anima.grounding.gather_evidence',return_value=[]):
        value,trace=run_grounded(provider,original,'txt2img',HINTS,'local','assembly')
    return value,trace,provider

def plan(**kwargs):
    return {'rejected':False,'english_translation':'a pink-haired girl around 14 years old',
            'subject_type':'person','age_group':'minor','generation':{'steps':32,'cfg':4.4},**kwargs}

def envelope(text='A pink-haired girl around 14 years old is the subject.'):
    return {'xml_out':'1girl, pink hair\n\n'+text,'text_out':''}

def test_plan_owns_metadata_assembly_cannot_age_up_or_override_sampling():
    value,trace,_=run([plan(),{'rejected':False,'age_group':'adult','generation':{'steps':40},
                             'english_translation':'a different subject','formatter':envelope()}, {'supported':True}])
    assert value['age_group']=='minor'
    assert value['generation']=={'steps':32,'cfg':4.4}
    assert value['english_translation']=='a pink-haired girl around 14 years old'

def test_visual_stages_never_receive_execution_metadata():
    _,_,provider=run([plan(aspect='portrait'),envelope(),{'supported':True}])
    for call in provider.send.call_args_list[1:]:
        payload=json.loads(call.args[0][1]['content'])
        assert 'generation' not in payload and 'request_requirements' not in payload and 'aspect' not in payload
        assert 'cfg' not in call.args[0][1]['content']

def test_corrected_prompt_is_independently_verified():
    value,trace,provider=run([plan(),envelope('The girl has white hair.'),
        {'supported':False,'issues':[{'quote':'white hair','problem':'wrong color'}],'formatter':envelope()}, {'supported':True}])
    assert trace['stage']=='semantically_revised' and trace['audit_passes']==2
    recheck=json.loads(provider.send.call_args_list[-1].args[0][1]['content'])
    assert recheck['draft_formatter']==envelope()
    assert 'white hair' not in value['formatter']['xml_out']

def test_unquotable_rejection_keeps_draft_and_never_injects_a_correction():
    value,trace,_=run([plan(),envelope('Pink hair, around 14 years old.'),
        {'supported':False,'issues':[{'quote':'mole_on_collarbone','problem':'imagined defect'}],
         'formatter':envelope('Red hair.')}])
    assert value['formatter']==envelope('Pink hair, around 14 years old.')
    assert trace['stage']=='fact_checked_unquoted_rejection' and trace['audit_unquoted_rejections']==1
    positive=compile_response(value,'a pink-haired girl around 14 years old',hints=HINTS)['data']['positive_prompt']
    assert 'Red hair' not in positive and 'Pink hair' in positive


def test_correction_that_drops_facts_is_replaced_by_bounded_repair():
    facts=[{'source_text':'a pink-haired girl around 14 years old','english':'a pink-haired girl around 14 years old'}]
    repair={'xml_out':'1girl, pink hair\n\nA pink-haired girl around 14 years old.','text_out':''}
    value,trace,provider=run([plan(visual_facts=facts),
        envelope('A pink-haired girl around 14 years old in a split screen.'),
        {'supported':False,'issues':[{'quote':'split screen','problem':'unrequested'}],'formatter':{'xml_out':'1girl','text_out':''}},
        repair,{'supported':True}])
    assert value['formatter']==repair and trace['stage']=='semantically_revised'
    repair_payload=json.loads(provider.send.call_args_list[-2].args[0][1]['content'])
    assert repair_payload['verified_issues'][0]['quote']=='split screen'

def test_supported_audit_cannot_smuggle_an_alternate_formatter():
    value,_,_=run([plan(),envelope(),{'supported':True,'formatter':envelope('A different character.')}])
    assert value['formatter']==envelope()

def test_invalid_assembly_is_retried_once_then_falls_back():
    value,trace,provider=run([plan(),'{"xml_out":broken}','{"xml_out":broken}'])
    assert value['_literal_fallback'] and provider.send.call_count==3
    assert trace['stage']=='literal_fallback_no_verified_assembly'


def test_degenerate_assembly_json_recovers_on_retry():
    value,trace,provider=run([plan(),'}]}]}',json.dumps(envelope()),{'supported':True}])
    assert value['formatter']==envelope() and trace['stage']=='fact_checked'
    assert provider.send.call_count==4

def test_recovery_cannot_reintroduce_unverified_formatter_or_metadata():
    provider=Mock();provider.send.side_effect=['not valid JSON','not valid JSON',json.dumps({
        'rejected':False,'english_translation':'crispy chicken wishbones',
        'formatter':envelope('A dragon in a castle.'),'tags':['dragon'],'description':'castle',
        'artists':['another_artist'],'age_group':'adult','generation':{'steps':40}})]
    with patch('anima.prompting.request_hints',return_value=HINTS):
        result=convert(provider,'crispy chicken wishbones','txt2img')
    data=result['data']
    assert data['formatter_source']=='literal_fallback'
    assert 'crispy chicken wishbones' in data['positive_prompt']
    assert 'dragon' not in data['positive_prompt'] and 'castle' not in data['positive_prompt']
    assert data['generation']['steps']==30 and data['artists']==[]

def test_recovery_moderation_still_blocks_forbidden_content():
    provider=Mock();provider.send.side_effect=['invalid','invalid',json.dumps({'rejected':True,'reason':'forbidden'})]
    with patch('anima.prompting.request_hints',return_value=HINTS):
        result=convert(provider,'test','txt2img')
    assert result['rejected'] and not result['success']

@pytest.mark.parametrize('raw',['{"rejected":false,"rejected":true}',
    '{"rejected":false} trailing text','```json\n{"rejected":false}\n```','[]'])
def test_strict_json_contract_rejects_duplicate_or_salvaged_responses(raw):
    with pytest.raises((ValueError,TypeError)):parse_stage_json(raw)

def test_visual_fact_ledger_is_request_scoped_not_a_global_character_profile():
    facts=[{'source_text':'pink-haired','english':'pink hair'},
           {'source_text':'not in request','english':'blue dress'}]
    _,_,provider=run([plan(visual_facts=facts),envelope(),{'supported':True}])
    for call in provider.send.call_args_list[1:]:
        facts=json.loads(call.args[0][1]['content'])['explicit_visual_facts']
        assert facts==[{'source_text':'pink-haired','english':'pink hair'}]

def test_exact_age_can_be_restored_by_semantic_repair_not_regex_clipping():
    value,trace,_=run([plan(),envelope('A minor girl.'),
        {'supported':False,'issues':[{'quote':'A minor girl','problem':'exact age missing'}],'formatter':envelope()}, {'supported':True}])
    assert 'around 14 years old' in value['formatter']['xml_out']
    assert trace['stage']=='semantically_revised'

def test_user_requested_visible_parameter_text_is_not_keyword_filtered():
    text='A sign reads steps: 30, CFG: 4.3.'
    value,_,_=run([plan(english_translation=text),envelope(text),{'supported':True}],original=text)
    assert 'steps: 30, CFG: 4.3' in compile_response(value,text,hints=HINTS)['data']['positive_prompt']

def test_unrequested_split_screen_is_revised_semantically():
    original='two women in a romantic illustration'
    fixed={'xml_out':'2girls, yuri\n\nTwo women share a romantic illustration.','text_out':''}
    value,trace,_=run([plan(english_translation=original),envelope('A left-right split screen.'),
        {'supported':False,'issues':[{'quote':'left-right split screen','problem':'unrequested split screen'}],'formatter':fixed}, {'supported':True}],original)
    assert 'split screen' not in value['formatter']['xml_out'] and trace['stage']=='semantically_revised'

def test_upstream_contract_and_custom_addition_mechanism_are_used():
    from anima.vendor.newbie_formatter.output_contract import MARKER
    assert MARKER in SYSTEM
    assert 'system_prompt_extra_anima' in __import__('inspect').getsource(__import__('anima.formatter_adapter',fromlist=['build_system']).build_system)
    assert 'Composition, lighting and sentence-count checklists' in SYSTEM


def test_upstream_scene_examples_and_default_staging_do_not_enter_system():
    from anima.formatter_adapter import scoped_format_spec,upstream_source_rules
    spec=scoped_format_spec()
    assert 'The image is divided into a left side and a right side' not in SYSTEM
    assert 'Character A holds Character B' not in SYSTEM
    assert 'railway station' not in SYSTEM
    assert '```' not in spec
    assert len(spec)<3500
    rules=upstream_source_rules()
    assert 'NSFW' not in rules and 'Never begins with sorry' not in rules
    assert '1girl' in rules


def test_requested_layout_is_not_deleted_by_source_spec_selection():
    original='two women in a left-right split screen, with blue light'
    direct={'xml_out':'2girls\n\n'+original,'text_out':''}
    value,trace,_=run([plan(english_translation=original),direct,{'supported':True}],original)
    assert 'left-right split screen' in value['formatter']['xml_out']
    assert 'blue light' in value['formatter']['xml_out']


def test_structured_schema_is_opt_in_and_uses_upstream_envelope():
    from anima.grounding import _json_response
    from anima.vendor.newbie_formatter.output_contract import SCHEMA
    class Provider:
        def __init__(self):self.calls=[]
        def send_json(self,messages,**kwargs):
            self.calls.append(kwargs)
            return json.dumps(envelope())
    provider=Provider()
    assert _json_response(provider,[],"anima-grounded-assembly-v2",20)==envelope()
    options=provider.calls[0]
    assert options['temperature']==0.0
    assert options['response_format']['json_schema']['schema']==SCHEMA
    assert options['response_format']['json_schema']['strict'] is True


def test_only_explicitly_unsupported_structured_format_retries_plain_transport():
    from anima.grounding import _json_response
    class Unsupported(Exception):status_code=400
    class Provider:
        def __init__(self,error):self.error=error;self.plain=0
        def send_json(self,*a,**kw):raise self.error
        def send(self,*a,**kw):self.plain+=1;return '{"rejected":false}'
    p=Provider(Unsupported('response_format unsupported'))
    assert _json_response(p,[],"plan",20)=={'rejected':False} and p.plain==1
    p=Provider(RuntimeError('service unavailable'))
    with pytest.raises(RuntimeError):_json_response(p,[],"plan",20)
    assert p.plain==0


def test_native_json_bridge_preserves_normal_send_defaults_without_loading_database():
    from types import SimpleNamespace
    from llm.core.native_model_bridge import NativePoolProvider
    provider=NativePoolProvider()
    result=SimpleNamespace(content='{}',native_result=SimpleNamespace())
    with patch('llm.core.native_model_bridge.shell_generate',return_value=result) as shell:
        provider.send([{'role':'user','content':'normal'}],timeout=10)
        assert shell.call_args.kwargs=={'tools':None,'timeout':10}
        provider.send_json([],response_format={'type':'json_object'},timeout=12)
        assert shell.call_args.kwargs=={'response_format':{'type':'json_object'},'temperature':0.0,'timeout':12}


def test_ungrounded_minor_rejection_is_reasked_once_then_accepted():
    provider=Mock();provider.send.side_effect=[json.dumps({'rejected':True,'reason':'minor sexualization'}),
        json.dumps(plan()),json.dumps(envelope()),json.dumps({'supported':True})]
    with patch('anima.grounding.gather_evidence',return_value=[]):
        value,trace=run_grounded(provider,'a 14-year-old girl in yukata at a fireworks festival','txt2img',HINTS,'local','assembly')
    assert value['rejected'] is False and 'formatter' in value
    assert provider.send.call_count==4
    retry=json.loads(provider.send.call_args_list[1].args[0][1]['content'])
    assert retry['rejection_audit'] and retry['previous_rejection']=='minor sexualization'


def test_grounded_rejection_is_not_retried():
    provider=Mock();provider.send.side_effect=[json.dumps({'rejected':True,'reason':'explicit act',
        'rejected_reason_quote':'裸体'})]
    with patch('anima.grounding.gather_evidence',return_value=[]):
        value,trace=run_grounded(provider,'裸体女孩','txt2img',HINTS,'local','assembly')
    assert value['rejected'] and trace['stage']=='planning_rejected'
    assert provider.send.call_count==1


def test_artist_shaped_tag_is_not_kept_as_a_visual_tag():
    raw={'rejected':False,'english_translation':'two girls','formatter':{'xml_out':'anmi@, 2girls\n\nTwo girls stand together.','text_out':''}}
    value=compile_response(raw,'two girls')['data']['positive_prompt']
    assert 'anmi@' not in value and '2girls' in value
