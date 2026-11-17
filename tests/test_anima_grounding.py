"""Grounding pipeline regressions; external services mocked except explicit live scripts."""
import json
from unittest.mock import Mock, patch

from anima.grounding import run_grounded, gather_evidence, _scope_record
from anima.prompting import compile_response


def plan():
    return {"rejected":False,"english_translation":"a girl with pink hair and blue eyes","subject_type":"person","dimensions":[]}


def draft(text="The girl has white hair and red eyes."):
    return {"rejected":False,"english_translation":"a girl with pink hair and blue eyes",
            "formatter":{"xml_out":"1girl\n\n"+text,"text_out":""}}


def test_retrieval_reaches_both_assembly_and_semantic_auditor():
    evidence=[{"evidence_id":"tool_0","scope":{"kind":"user_requested_dimension","source_text":"粉发蓝眼"},"data":{"result":{"results":[{"tag":"pink_hair"},{"tag":"blue_eyes"}]}},"status":"available"}]
    corrected={"xml_out":"1girl, pink hair, blue eyes\n\nThe girl has pink hair and blue eyes.","text_out":""}
    provider=Mock();provider.send.side_effect=[json.dumps(plan()),json.dumps(draft()),json.dumps({"supported":False,"issues":[{"quote":"white hair and red eyes","problem":"contradicts request"}],"formatter":corrected}),json.dumps({"supported":True})]
    with patch('anima.grounding.gather_evidence',return_value=evidence):
        value,trace=run_grounded(provider,'粉发蓝眼女孩','txt2img',{'artists':[],'characters':[]},'local','assembly')
    assert trace['stage']=='semantically_revised'
    for call in provider.send.call_args_list[1:]:
        payload=json.loads(call.args[0][1]['content'])
        assert payload['scoped_evidence'][0]['evidence_id']=='tool_0'
    compiled=compile_response(value,'粉发蓝眼女孩')['data']['positive_prompt']
    assert 'pink hair and blue eyes' in compiled and 'white hair' not in compiled


def test_unavailable_fact_audit_reverts_to_original_not_a_sentence_filter():
    provider=Mock();provider.send.side_effect=[json.dumps(plan()),json.dumps(draft()),TimeoutError()]
    with patch('anima.grounding.gather_evidence',return_value=[]):
        value,trace=run_grounded(provider,'粉发蓝眼女孩','txt2img',{'artists':[],'characters':[]},'local','assembly')
    assert trace['stage']=='literal_fallback_no_verified_assembly'
    assert 'formatter' not in value
    compiled=compile_response(value,'粉发蓝眼女孩')['data']['positive_prompt']
    assert '粉发蓝眼女孩' in compiled and 'white hair' not in compiled


def test_supported_appearance_prose_is_not_cut_even_when_contains_hair_and_eyes():
    text='The girl has pink hair and blue eyes. Her clothing is a white yukata.'
    provider=Mock();provider.send.side_effect=[json.dumps(plan()),json.dumps(draft(text)),json.dumps({'supported':True})]
    with patch('anima.grounding.gather_evidence',return_value=[]):
        value,trace=run_grounded(provider,'粉发蓝眼女孩穿白浴衣','txt2img',{'artists':[],'characters':[]},'local','assembly')
    assert trace['stage']=='fact_checked' and value['formatter']['xml_out'].endswith(text)


def test_planning_rejection_does_not_search_or_format():
    provider=Mock();provider.send.return_value=json.dumps({'rejected':True,'reason':'blocked','rejected_reason_quote':'test'})
    with patch('anima.grounding.gather_evidence') as gather:
        value,trace=run_grounded(provider,'test','txt2img',{'artists':[],'characters':[]},'local','assembly')
    assert value['rejected'] and trace['stage']=='planning_rejected'
    gather.assert_not_called();provider.send.assert_called_once()


def test_exact_character_search_candidates_are_scoped_before_assembly():
    result={'tool':'search_tags','result':{'results':[{'tag':'eyjafjalla_(arknights)'},{'tag':'goldenglow_(arknights)'}]}}
    with patch('anima.grounding.call_tag_tool',return_value=result):
        record=_scope_record({'kind':'character_identity','canonical_tag':'eyjafjalla (arknights)'},'search_tags',{'query':'test'},0)
    assert record['data']['result']['results']==[{'tag':'eyjafjalla_(arknights)'}]
    assert record['data']['result']['unrelated_candidates_omitted']==1


def test_artist_profile_is_not_flattened_into_character_facts():
    hints={'artists':['anmi'],'characters':[]}
    result={'result':{'top_tags':[{'tag':'gun'},{'tag':'water'}]}}
    with patch('anima.grounding.call_tag_tool',return_value=result):
        records=gather_evidence('@anmi',{'dimensions':[]},hints)
    assert records[0]['scope']['kind']=='artist_style_reference'
    assert records[0]['scope']['not_character_facts'] is True


def test_unrelated_search_dimension_not_sent_to_mcp():
    with patch('anima.grounding.call_tag_tool') as tool:
        # Private original text is not used as a fallback web query.
        records=gather_evidence('contact test@example.org',{'dimensions':[{'source_text':'not present','query':'private topic'}]},{'artists':[],'characters':[]})
    assert records==[]
    tool.assert_not_called()


def test_nested_mcp_result_normalized_and_cache_preserves_shape():
    from anima import tag_tools
    tag_tools._CACHE.clear()
    upstream=json.dumps({'result':json.dumps({'results':[{'tag':'pink_hair'}]})})
    args={'query':'cache_regression','search_mode':'precise_lookup','category':'general'}
    with patch.object(tag_tools.upstream,'execute_search_tags',return_value=upstream) as search:
        first=tag_tools.call_tag_tool('search_tags',args)
        first['result']['results'].clear()
        second=tag_tools.call_tag_tool('search_tags',args)
    assert second['result']['results']==[{'tag':'pink_hair'}] and second['cache_hit']
    search.assert_called_once()
    tag_tools._CACHE.clear()
