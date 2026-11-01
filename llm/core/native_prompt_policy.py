"""Inherit style from this checkout's native defaults without importing runtime."""
import ast
from pathlib import Path


def native_styles():
    path = Path(__file__).resolve().parents[2] / 'src/config/official_configs.py'
    tree = ast.parse(path.read_text(encoding='utf8'))
    values = {}
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name in {'PersonalityConfig', 'ChatReplyStyleConfig'}:
            for item in cls.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    try:
                        node = item.value
                        if isinstance(node, ast.Call):
                            defaults = {k.arg: k.value for k in node.keywords}
                            node = defaults.get('default')
                            if node is None and isinstance(defaults.get('default_factory'), ast.Lambda):
                                node = defaults['default_factory'].body
                        values[item.target.id] = ast.literal_eval(node)
                    except (ValueError, TypeError, StopIteration):
                        pass
    # The upstream persona mixes identity/temperament with a conversational
    # behavior sentence. Keep the custom identity and temperament, but retain
    # that native scene guidance in both Planner and Replyer. Replyer does not
    # read behavior_style, so copying it only there silently loses the guidance.
    behavior = values['behavior_style']
    prefix = values['personality']
    scene_style = ''.join(sentence + '。' for sentence in prefix.split('。')
                          if sentence.startswith('善于') and '聊天' in sentence)
    if behavior.startswith(prefix):
        behavior = scene_style + behavior[len(prefix):]
    return {
        'behavior_style': behavior,
        'reply_style': scene_style + values['reply_style'],
        **{k: values[k] for k in ('multiple_reply_style', 'multiple_probability',
                                 'group_chat_prompt', 'private_chat_prompts')},
    }


def profile_prompt(llm, profile):
    """Compatibility API: shell profiles no longer supply any LLM instructions."""
    return {}
