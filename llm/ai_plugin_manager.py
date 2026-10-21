"""Validated CRUD and bounded execution for AI-authored message plugins."""
from __future__ import annotations
import ast
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import types
from datetime import datetime, timezone
from pathlib import Path
from llm.ai_plugin_runtime import Interpreter, validate_tree, bound

BASE_DIR = Path(__file__).resolve().parents[1]
AI_PLUGIN_DIR = BASE_DIR / "plugins" / "ai_generated"
AI_PLUGIN_MANIFEST = BASE_DIR / "data" / "ai_plugins.json"
MAX_SOURCE_CHARS = 16000
MAX_RETURN_CHARS = 4000
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
_COMMAND_RE = re.compile(r"^/[^\s/\\\x00-\x1f]{1,60}$")
_LOCK = threading.RLock()
PLUGIN_DEVELOPMENT_SPEC = """AI PLUGIN DEVELOPMENT GUIDE (experimental message-only Python subset)
1. Call action=guide before create/update. Use list/get to inspect existing AI plugins.
   validate accepts supplied source without writing; update replaces the full source.
   create/update/delete require confirm=true reflecting an explicit user request.
2. Source is UTF-8 without BOM, LF newlines, at most 16000 characters. Declare:
   COMMAND = "/hello_ai"
   ALIASES = ["/hi_ai"]  # optional
   def handle(content, context):
       name = context.get("user") or "friend"
       return {"messages": ["Hello, " + name + ": " + (content or "")], "mode": "wechat_text"}
3. Exactly two positional arguments; content is the command arguments as text.
   context provides only user/group/content/args/mentions/prefix_used. No raw/config/keys.
4. Return None, text, or {"content":"text"} or {"messages":["text"],"mode":"wechat_text"}.
   At most 8 messages / 4000 total characters. No target/file_path/animation/forward flags.
   URLs may be returned as literal text, but NEVER fetched by a plugin. Do not emit
   native mention markers, @everyone, or commands that would trigger another bot.
5. Supported: literals, local assignments, if/else, simple f-strings, slicing, arithmetic,
   bounded for loops, helper functions, range/len/str/int/float/bool/min/max/sum/sorted,
   dict.get/items/keys/values, text strip/lower/upper/split/join/replace/startswith/isdigit,
   list.append. Avoid comprehensions: use a bounded for loop and append instead.
   No imports (including random/re/json), open/files, network, process, eval/exec,
   reflection, classes, decorators, lambda, while, try/raise, globals, or persistence.
   Limits: 256 collection items, 8192 intermediate text chars, 4000 evaluation steps,
   8 function-call levels. This is a bounded AST interpreter; it does NOT exec Python.
6. handle must tolerate empty content and return a valid result. Validation runs empty,
   normal and Unicode sample inputs before any file change. On error read error_code
   and validation errors, repair source and retry; never claim success from ok=false.
   Existing files remain intact on validation failure. Only AI-plugin files may be edited.
7. Tool results continue the same conversation. A source commit is not a chat reply;
   after the tool confirms success, answer the user using the normal messages JSON.
   Never expose secrets, internal account identifiers or file paths in that reply.
"""


def _plugin_path(name):
    if not isinstance(name,str) or not _NAME_RE.fullmatch(name):
        raise ValueError("name must match [a-z][a-z0-9_]{0,48}; do not supply paths")
    root=AI_PLUGIN_DIR.absolute()
    if root.is_symlink(): raise ValueError("AI plugin directory cannot be a symlink")
    path=root / (name+'.py')
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("Plugin path escaped the AI-plugin directory")
    return path


def _atomic_write(path, text):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=str(path.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def _sanitize(context):
    result={}
    for key in ('user','group','content','args','mentions','prefix_used'):
        val=(context or {}).get(key)
        if isinstance(val,str):result[key]=val[:2000]
        elif isinstance(val,bool):result[key]=val
        elif key=='mentions' and isinstance(val,list):result[key]=[str(x)[:100] for x in val[:12]]
    return result


def validate_plugin_result(result):
    if result is None:return {'ok':True,'value':None}
    if isinstance(result,str): messages=[result]
    elif isinstance(result,dict):
        extra=set(result)-{'content','messages','mode','delay_seconds'}
        if extra:return {'ok':False,'error':'Forbidden return fields: '+', '.join(sorted(extra))}
        if 'content' in result and 'messages' in result:return {'ok':False,'error':'Return content OR messages, not both'}
        if not any(k in result for k in ('content','messages')):return {'ok':False,'error':'Return dict requires content or messages'}
        messages=result.get('messages',[result.get('content','')])
        if result.get('mode','wechat_text')!='wechat_text':return {'ok':False,'error':'Only wechat_text mode is permitted'}
        delay=result.get('delay_seconds',0)
        if isinstance(delay,bool) or not isinstance(delay,(int,float)) or not math.isfinite(delay) or not 0<=delay<=60:
            return {'ok':False,'error':'delay_seconds must be finite and between 0 and 60'}
    else:return {'ok':False,'error':'Return None, text, or a text result dictionary'}
    if not isinstance(messages,list) or len(messages)>8 or any(not isinstance(x,str) for x in messages):
        return {'ok':False,'error':'messages must be a list of at most 8 strings'}
    if sum(len(x) for x in messages)>MAX_RETURN_CHARS:return {'ok':False,'error':'Total returned text exceeds 4000 characters'}
    if any('[[WX_AT' in x or x.lstrip().startswith('/') or re.search(r'@(?:all|everyone|\u6240\u6709\u4eba)',x,re.I) for x in messages):
        return {'ok':False,'error':'Native mention markers, mass mentions, and outbound bot commands are not permitted'}
    return {'ok':True,'value':result}


def validate_plugin_source(name,source,*,command_exists=None):
    result={'ok':False,'name':name,'errors':[],'warnings':[],'command':None,'aliases':[]}
    def fail(text):result['errors'].append(text)
    try:_plugin_path(name)
    except ValueError as e:fail(str(e));return result
    if not isinstance(source,str):fail('source must be a complete Python source string');return result
    if source.startswith('\ufeff'):fail('BOM forbidden: provide UTF-8 without BOM')
    if '\x00' in source or len(source)>MAX_SOURCE_CHARS:fail('Source contains NUL or exceeds 16000 characters')
    source=source.replace('\r\n','\n').replace('\r','\n')
    if not source.endswith('\n'):source+='\n'
    try:
        source.encode('utf-8',errors='strict')
        tree=ast.parse(source,filename=name+'.py')
        compile(tree,name+'.py','exec')  # syntax validation ONLY, never executed
    except (ValueError,SyntaxError,UnicodeError,RecursionError) as e:
        fail('Invalid Python/UTF-8 source: '+str(e)[:180]);return result
    if len(list(ast.walk(tree)))>1800:fail('AST size limit exceeded');return result
    result['errors'].extend(validate_tree(tree))
    constants={}; functions=[]
    for node in tree.body:
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            try:
                value=bound(ast.literal_eval(node.value))
                for target in (node.targets if isinstance(node,ast.Assign) else [node.target]):
                    if not isinstance(target,ast.Name):raise ValueError('Top-level assignments require names')
                    if target.id in constants:raise ValueError('Duplicate top-level name: '+target.id)
                    constants[target.id]=value
            except (ValueError,TypeError,RecursionError) as e:fail('Top-level values must be bounded literals: '+str(e)[:120])
        elif isinstance(node,ast.FunctionDef):functions.append(node)
        elif isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):pass
        else:fail('Module top level supports only literal declarations, docstrings, and functions')
    names=[n.name for n in functions]
    if len(set(names))!=len(names) or set(names)&set(constants):fail('Duplicate or conflicting function/constant names')
    command=constants.get('COMMAND');aliases=constants.get('ALIASES',[])
    if not isinstance(command,str) or not _COMMAND_RE.fullmatch(command):fail("Declare a whitespace-free COMMAND='/name'")
    else:result['command']=command
    if not isinstance(aliases,(list,tuple)) or any(not isinstance(x,str) or not _COMMAND_RE.fullmatch(x) for x in aliases):
        fail('ALIASES must be a list of whitespace-free /commands')
    else:result['aliases']=list(aliases)
    declared=([command] if isinstance(command,str) else [])+result['aliases']
    if len(set(declared))!=len(declared):fail('COMMAND and ALIASES must be unique')
    for c in declared:
        if isinstance(c,str) and command_exists and command_exists(c,name):fail('Command conflict: '+c)
    handler=next((n for n in functions if n.name=='handle'),None)
    if handler is None:fail('Provide def handle(content, context)')
    elif len(handler.args.args+handler.args.posonlyargs)!=2 or handler.args.kwonlyargs:
        fail('handle requires exactly two positional arguments: content and context')
    if not result['errors']:
        for content in ('','test','\u4e2d\u6587\U0001f600'):
            try:
                value=Interpreter(tree).call('handle',[content,{'user':'\u6d4b\u8bd5\u7528\u6237','content':content,'group':'test','mentions':[]}])
                check=validate_plugin_result(value)
                if not check['ok']:raise ValueError(check['error'])
            except Exception as e:fail('Runtime/return preflight failed for '+repr(content)+': '+str(e)[:160]);break
    result['errors']=list(dict.fromkeys(result['errors']))[:20]
    result['ok']=not result['errors']
    if result['ok']:result['normalized_source']=source
    return result


class AIPluginManager:
    def __init__(self,config=None,reload_callback=None):
        self.config=config or {};self.reload_callback=reload_callback;self.lock=_LOCK
        AI_PLUGIN_DIR.mkdir(parents=True,exist_ok=True)

    def set_reload_callback(self,callback):self.reload_callback=callback

    def _manifest(self):
        if not AI_PLUGIN_MANIFEST.exists():return {}
        value=json.loads(AI_PLUGIN_MANIFEST.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError('Invalid AI-plugin manifest')
        return value

    def _command_exists(self,command,current_name=''):
        paths=list((BASE_DIR/'plugins').glob('*.py'))+list(AI_PLUGIN_DIR.glob('*.py'))
        for p in paths:
            if p.parent==AI_PLUGIN_DIR and p.stem==current_name:continue
            try:
                tree=ast.parse(p.read_text(encoding='utf-8-sig'))
                values={}
                for n in tree.body:
                    if isinstance(n,ast.Assign):
                        for t in n.targets:
                            if isinstance(t,ast.Name) and t.id in ('COMMAND','ALIASES'):
                                values[t.id]=ast.literal_eval(n.value)
                commands=[values.get('COMMAND','/'+p.stem)]+list(values.get('ALIASES') or [])
                if command in commands:return True
            except (ValueError,TypeError,SyntaxError,UnicodeError):continue
        return False

    def _read_source(self,name):return _plugin_path(name).read_text(encoding='utf-8')

    def list_plugins(self,include_source=False):
        with self.lock:
            manifest=self._manifest();plugins=[]
            for p in sorted(AI_PLUGIN_DIR.glob('*.py')):
                try:
                    source=self._read_source(p.stem)
                    check=validate_plugin_source(p.stem,source,command_exists=self._command_exists)
                    item={'name':p.stem,'ai_developed':True,'command':check.get('command'),'aliases':check.get('aliases',[]),
                          'description':manifest.get(p.stem,{}).get('description',''),'status':'valid' if check['ok'] else 'invalid'}
                    if include_source:item['source']=source
                    if not check['ok']:item['errors']=check['errors']
                except (OSError,ValueError) as e:item={'name':p.stem,'status':'invalid','errors':[str(e)[:120]],'ai_developed':True}
                plugins.append(item)
            return {'ok':True,'plugins':plugins}

    def _reload(self):
        if not self.reload_callback:return {'ok':True,'reloaded':False,'note':'Validated source is loaded on the next matching command'}
        return self.reload_callback()

    def handle_tool(self,payload):
        try:
            with self.lock:return self._handle(payload)
        except Exception as e:return {'ok':False,'error_code':'PLUGIN_OPERATION_FAILED','error':str(e)[:250],'hint':'Call guide, inspect errors, repair source and retry.'}

    def _handle(self,payload):
        if not isinstance(payload,dict):raise ValueError('Arguments must be a JSON object')
        action=payload.get('action','guide');name=payload.get('name','')
        if action=='guide':return {'ok':True,'guide':PLUGIN_DEVELOPMENT_SPEC,'limits':{'source_chars':MAX_SOURCE_CHARS,'return_chars':MAX_RETURN_CHARS}}
        if action=='list':return self.list_plugins(payload.get('include_source') is True)
        if action not in {'get','validate','create','update','delete'}:raise ValueError('Unsupported action; use guide/list/get/validate/create/update/delete')
        path=_plugin_path(name)
        if action=='get':return {'ok':True,'name':name,'ai_developed':True,'source':self._read_source(name)}
        if action=='validate':
            source=payload.get('source') if 'source' in payload else self._read_source(name)
            check=validate_plugin_source(name,source,command_exists=self._command_exists)
            return {**{k:v for k,v in check.items() if k!='normalized_source'},'error_code':None if check['ok'] else 'VALIDATION_FAILED','hint':'Call guide for supported syntax; no file was changed.'}
        if payload.get('confirm') is not True:raise ValueError('Mutation requires confirm=true and explicit user intent')
        if action=='create' and path.exists():raise ValueError('Plugin exists; use get/update')
        if action in {'update','delete'} and not path.exists():raise ValueError('AI plugin does not exist')
        manifest=self._manifest();old_source=path.read_text(encoding='utf-8') if path.exists() else None
        old_manifest=dict(manifest)
        if action=='delete':manifest.pop(name,None)
        else:
            check=validate_plugin_source(name,payload.get('source'),command_exists=self._command_exists)
            if not check['ok']:return {'ok':False,'error_code':'VALIDATION_FAILED','validation':{k:v for k,v in check.items() if k!='normalized_source'},'hint':'Read guide and repair errors; no file was changed.'}
            source=check['normalized_source']
            description=payload.get('description',manifest.get(name,{}).get('description',''))
            if not isinstance(description,str):raise ValueError('description must be text')
            manifest[name]={'ai_developed':True,'command':check['command'],'aliases':check['aliases'],'description':description[:240],
                            'updated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),'sha256':hashlib.sha256(source.encode('utf-8')).hexdigest()}
        try:
            if action=='delete':path.unlink()
            else:
                _atomic_write(path,source)
                if path.read_bytes()!=source.encode('utf-8'):raise ValueError('Written UTF-8 bytes differ from validated source')
                module=self.load_module(name)
                if module.COMMAND!=check['command']:raise ValueError('Reloaded command mismatch')
            _atomic_write(AI_PLUGIN_MANIFEST,json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
            reload_result=self._reload()
            if isinstance(reload_result,dict) and reload_result.get('ok') is False:raise ValueError('Reload callback rejected plugin')
        except Exception:
            if old_source is None:
                if path.exists():path.unlink()
            else:_atomic_write(path,old_source)
            _atomic_write(AI_PLUGIN_MANIFEST,json.dumps(old_manifest,ensure_ascii=False,indent=2)+'\n')
            raise
        return {'ok':True,'action':action,'name':name,'ai_developed':True,'command':manifest.get(name,{}).get('command'),
                'validation':{'encoding':'utf-8-no-bom','syntax':True,'return_contract':True,'bounded_runtime':True},'reloaded':reload_result}

    def load_module(self,name):
        source=self._read_source(name)
        check=validate_plugin_source(name,source,command_exists=self._command_exists)
        if not check['ok']:raise ValueError('; '.join(check['errors']))
        tree=ast.parse(check['normalized_source'])
        return types.SimpleNamespace(COMMAND=check['command'],ALIASES=check['aliases'],__name__='plugins.ai_generated.'+name,
                                     handle=lambda content,context:Interpreter(tree).call('handle',[str(content or '')[:2000],_sanitize(context)]))

    def execute(self,name,content,context):
        try:
            result=self.load_module(name).handle(content,context)
            return validate_plugin_result(result)
        except Exception as e:return {'ok':False,'error_code':'EXECUTION_FAILED','error':type(e).__name__+': '+str(e)[:200]}
