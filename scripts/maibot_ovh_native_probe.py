"""Live native-host tool and vision probe, synthetic inputs and isolated state."""
import asyncio
import argparse
import base64
from datetime import datetime
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE = Path(tempfile.mkdtemp(prefix='ovh-native-',dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR'] = str(INSTANCE)
(INSTANCE/'config').mkdir()
for name in ['bot_config.toml','model_config.toml']:
    shutil.copy2(ROOT/'config'/name,INSTANCE/'config'/name)


async def main(selected_model='', output='ovh-native-20261029.json'):
    from src.common.data_models.llm_service_data_models import LLMGenerationOptions
    from src.config.config import config_manager
    from src.services.llm_service import LLMServiceClient
    from src.llm_models.payload_content.context_item import ContextItemBuilder
    image = io.BytesIO()
    Image.new('RGB',(64,64),(255,0,0)).save(image,format='PNG')
    encoded_image = base64.b64encode(image.getvalue()).decode()
    def vision_context(client):
        builder = ContextItemBuilder().add_text_content(
            'Synthetic vision test: return only the dominant color in English.')
        # OVH rejects more than five images. Exercise the real per-provider cap.
        for _ in range(6):
            builder.add_image_content('png', encoded_image)
        return [builder.build()]
    tools = [dict(type='function',function=dict(name='health_probe',description='Synthetic health probe',
              parameters=dict(type='object',properties=dict(value=dict(type='string')),required=['value'])))]
    async def model_probe(model):
        client = LLMServiceClient('planner',request_type='maisaka.acceptance.synthetic')
        row = dict(model=model.name,checks=[])
        try:
            result = await client.generate_response('Synthetic test. Call health_probe with value ok. Do not answer directly.',
                     LLMGenerationOptions(model_name=model.name,max_tokens=2048,tool_options=tools))
            calls = result.tool_calls or []
            row['checks'].append(dict(kind='native_tool',passed=bool(calls) and calls[0].func_name=='health_probe'))
            result = await client.generate_response_with_context(
                vision_context, LLMGenerationOptions(model_name=model.name,max_tokens=2048))
            row['checks'].append(dict(kind='native_vision',passed='red' in result.response.lower()))
            row['image_input_count_before_provider_limit'] = 6
        except Exception as exc:
            row['error'] = type(exc).__name__ + ': ' + str(exc)[:300]
        row['passed'] = len(row['checks'])==2 and all(c['passed'] for c in row['checks'])
        return row
    models = [m for m in config_manager.get_model_config().models if m.name.startswith('wechat_qwen3_')
              and (not selected_model or m.name == selected_model)]
    rows = await asyncio.gather(*(model_probe(m) for m in models))
    report = dict(at=datetime.now().isoformat(),synthetic_only=True,physical_messages=False,instance=str(INSTANCE),
                  models=rows,passed=bool(rows) and all(r['passed'] for r in rows))
    (ROOT/'docs/maibot-wechat-acceptance'/output).write_text(json.dumps(report,indent=2),encoding='utf8')
    print(json.dumps(report),flush=True)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',default='')
    parser.add_argument('--output',default='ovh-native-20261029.json')
    args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.model,args.output)) else 1)
