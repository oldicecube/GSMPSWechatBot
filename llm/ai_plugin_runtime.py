"""Bounded interpreter for AI-authored message-only plugins.

This is a Python subset interpreter, not exec with a reduced builtins dictionary.
Only primitive message data and explicit text/collection operations are available.
"""
import ast
import math
import operator

MAX_ITEMS = 256
MAX_TEXT = 8192
MAX_STEPS = 4000
BUILTINS = {"str", "int", "float", "bool", "len", "abs", "round", "min", "max",
            "sum", "all", "any", "sorted", "list", "tuple", "set", "dict", "range",
            "enumerate", "zip"}
METHODS = {"get", "keys", "values", "items", "strip", "lstrip", "rstrip", "lower",
           "upper", "casefold", "capitalize", "title", "replace", "split", "rsplit",
           "splitlines", "join", "startswith", "endswith", "isdigit", "isalpha",
           "isalnum", "count", "find", "index", "append", "reverse"}
NODES = {"Module", "FunctionDef", "arguments", "arg", "Assign", "AnnAssign", "AugAssign",
         "Expr", "Return", "If", "IfExp", "For", "Break", "Continue", "Pass",
         "Name", "Load", "Store", "Constant", "List", "Tuple", "Dict", "Set",
         "Subscript", "Slice", "Attribute", "Call", "keyword", "JoinedStr", "FormattedValue",
         "BinOp", "UnaryOp", "BoolOp", "Compare", "Add", "Sub", "Mult", "Div",
         "FloorDiv", "Mod", "USub", "UAdd", "Not", "And", "Or", "Eq", "NotEq",
         "Lt", "LtE", "Gt", "GtE", "In", "NotIn", "Is", "IsNot"}
BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
       ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a,b: a in b,
       ast.NotIn: lambda a,b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not}

class PluginLimit(ValueError):
    pass

class _Return(Exception):
    def __init__(self, value): self.value = value
class _Break(Exception): pass
class _Continue(Exception): pass


def bound(value, depth=0, budget=None):
    # Count repeated references too: nested containers must remain cheap to
    # traverse and stringify, even when they share the same child objects.
    if budget is None: budget = [4096, MAX_TEXT * 4]
    budget[0] -= 1
    if budget[0] < 0: raise PluginLimit("Aggregate data size limit exceeded")
    if depth > 6: raise PluginLimit("Data nesting limit exceeded")
    if value is None or type(value) is bool: return value
    if type(value) is int:
        if value.bit_length() > 128: raise PluginLimit("Integer size limit exceeded")
        return value
    if type(value) is float:
        if not math.isfinite(value): raise PluginLimit("Non-finite number is forbidden")
        return value
    if type(value) is str:
        if len(value) > MAX_TEXT: raise PluginLimit("Text size limit exceeded")
        budget[1] -= len(value)
        if budget[1] < 0: raise PluginLimit("Aggregate text size limit exceeded")
        return value
    if type(value) in (list, tuple, set, range):
        if len(value) > MAX_ITEMS: raise PluginLimit("Collection size limit exceeded")
        if isinstance(value, range): return list(value)
        for item in value: bound(item, depth+1, budget)
        return value
    if type(value) is dict:
        if len(value) > MAX_ITEMS: raise PluginLimit("Dictionary size limit exceeded")
        for k,v in value.items(): bound(k,depth+1,budget); bound(v,depth+1,budget)
        return value
    raise PluginLimit("Only primitive message data is allowed")


def validate_tree(tree):
    errors = []
    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for node in ast.walk(tree):
        kind = type(node).__name__
        if kind not in NODES: errors.append(f"Unsupported syntax: {kind}. Use the guide's supported Python subset.")
        if isinstance(node, ast.Name) and node.id.startswith('_'):
            errors.append("Private/dunder names are forbidden")
        if isinstance(node, ast.Attribute):
            if node.attr not in METHODS: errors.append(f"Method/attribute not permitted: {node.attr}")
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list or node.args.defaults or node.args.kw_defaults or node.args.vararg or node.args.kwarg:
                errors.append("Decorators/default arguments/variadic functions are not supported")
            if node.name.startswith('_'): errors.append("Private function names are forbidden")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in BUILTINS | functions:
                    errors.append(f"Call not permitted: {node.func.id}. No imports, files, network, or process calls.")
            elif not isinstance(node.func, ast.Attribute): errors.append("Dynamic call targets are forbidden")
            if any(k.arg is None for k in node.keywords): errors.append("Keyword unpacking is forbidden")
        if isinstance(node, ast.Attribute):
            parent_call = any(isinstance(p,ast.Call) and p.func is node for p in ast.walk(tree))
            if not parent_call: errors.append("Attributes are allowed only as direct method calls")
        if isinstance(node, ast.Dict) and any(k is None for k in node.keys): errors.append("Dictionary unpacking is forbidden")
        if isinstance(node, ast.For) and node.orelse: errors.append("for/else is not supported")
        if isinstance(node, ast.FormattedValue) and node.format_spec: errors.append("Use simple f-string fields without format specifications")
    return list(dict.fromkeys(errors))[:20]


class Interpreter:
    def __init__(self, tree):
        self.tree = tree
        self.functions = {n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
        self.constants = {}
        self.steps = 0
        self.depth = 0
        for n in tree.body:
            if isinstance(n,(ast.Assign,ast.AnnAssign)):
                val = bound(ast.literal_eval(n.value))
                for target in (n.targets if isinstance(n,ast.Assign) else [n.target]):
                    if not isinstance(target,ast.Name): raise PluginLimit("Top-level assignment requires a name")
                    self.constants[target.id] = val

    def tick(self):
        self.steps += 1
        if self.steps > MAX_STEPS: raise PluginLimit("Execution step limit exceeded")

    def call(self, name, args):
        self.tick()
        if self.depth >= 8: raise PluginLimit("Function depth limit exceeded")
        fn = self.functions[name]
        params = fn.args.posonlyargs + fn.args.args
        if len(params)!=len(args) or fn.args.kwonlyargs: raise PluginLimit("Function argument count mismatch")
        env = dict(self.constants)
        env.update({p.arg:bound(v) for p,v in zip(params,args)})
        self.depth += 1
        try:
            self.block(fn.body,env)
        except _Return as r: return bound(r.value)
        finally: self.depth -= 1
        return None

    def bind(self, target, value, env):
        if isinstance(target,ast.Name): env[target.id] = bound(value)
        elif isinstance(target,(ast.Tuple,ast.List)):
            if len(target.elts)!=len(value): raise PluginLimit("Unpacking length mismatch")
            for t,v in zip(target.elts,value): self.bind(t,v,env)
        elif isinstance(target,ast.Subscript):
            obj=self.expr(target.value,env); key=self.expr(target.slice,env)
            if type(obj) not in (list,dict): raise PluginLimit("Assignment target is not a collection")
            obj[key]=bound(value);bound(obj)
        else: raise PluginLimit("Unsupported assignment target")

    def block(self, nodes, env):
        for n in nodes:
            self.tick()
            if isinstance(n,ast.Return): raise _Return(self.expr(n.value,env) if n.value else None)
            elif isinstance(n,(ast.Assign,ast.AnnAssign)):
                value=self.expr(n.value,env)
                for target in (n.targets if isinstance(n,ast.Assign) else [n.target]): self.bind(target,value,env)
            elif isinstance(n,ast.AugAssign): self.bind(n.target,self.bin(n.op,self.expr(n.target,env),self.expr(n.value,env)),env)
            elif isinstance(n,ast.Expr): self.expr(n.value,env)
            elif isinstance(n,ast.If): self.block(n.body if self.expr(n.test,env) else n.orelse,env)
            elif isinstance(n,ast.For):
                for value in self.expr(n.iter,env):
                    self.tick(); self.bind(n.target,value,env)
                    try:self.block(n.body,env)
                    except _Break:break
                    except _Continue:continue
            elif isinstance(n,ast.Break): raise _Break()
            elif isinstance(n,ast.Continue): raise _Continue()
            elif not isinstance(n,ast.Pass): raise PluginLimit("Unsupported statement")

    def bin(self, op, a, b):
        if isinstance(op,ast.Mult):
            seq, count = (a,b) if isinstance(a,(str,list,tuple)) else (b,a)
            if isinstance(seq,(str,list,tuple)) and isinstance(count,int):
                if len(seq)*max(0,count) > (MAX_TEXT if isinstance(seq,str) else MAX_ITEMS):
                    raise PluginLimit("Sequence repetition limit exceeded")
            if isinstance(a,int) and isinstance(b,int) and a.bit_length()+b.bit_length()>128:
                raise PluginLimit("Integer multiplication limit exceeded")
        if isinstance(op,ast.Mod) and isinstance(a,str): raise PluginLimit("Percent string formatting is not supported")
        return bound(BIN[type(op)](a,b))

    def expr(self, n, env):
        self.tick()
        if isinstance(n,ast.Constant): return bound(n.value)
        if isinstance(n,ast.Name):
            if n.id not in env: raise PluginLimit(f"Unknown variable: {n.id}")
            return env[n.id]
        if isinstance(n,(ast.List,ast.Tuple,ast.Set)):
            val=[self.expr(x,env) for x in n.elts]
            return bound(tuple(val) if isinstance(n,ast.Tuple) else set(val) if isinstance(n,ast.Set) else val)
        if isinstance(n,ast.Dict): return bound({self.expr(k,env):self.expr(v,env) for k,v in zip(n.keys,n.values)})
        if isinstance(n,ast.Subscript): return bound(self.expr(n.value,env)[self.expr(n.slice,env)])
        if isinstance(n,ast.Slice): return slice(*(self.expr(x,env) if x else None for x in (n.lower,n.upper,n.step)))
        if isinstance(n,ast.BinOp): return self.bin(n.op,self.expr(n.left,env),self.expr(n.right,env))
        if isinstance(n,ast.UnaryOp):
            v=self.expr(n.operand,env)
            return bound(not v if isinstance(n.op,ast.Not) else -v if isinstance(n.op,ast.USub) else +v)
        if isinstance(n,ast.BoolOp):
            v=None
            for x in n.values:
                v=self.expr(x,env)
                if isinstance(n.op,ast.And) and not v or isinstance(n.op,ast.Or) and v: break
            return v
        if isinstance(n,ast.IfExp): return self.expr(n.body if self.expr(n.test,env) else n.orelse,env)
        if isinstance(n,ast.Compare):
            a=self.expr(n.left,env)
            for op,x in zip(n.ops,n.comparators):
                b=self.expr(x,env)
                if not CMP[type(op)](a,b): return False
                a=b
            return True
        if isinstance(n,ast.JoinedStr): return bound(''.join(str(self.expr(x,env)) for x in n.values))
        if isinstance(n,ast.FormattedValue): return str(self.expr(n.value,env))
        if isinstance(n,ast.Call):
            args=[self.expr(x,env) for x in n.args]; kw={k.arg:self.expr(k.value,env) for k in n.keywords}
            if isinstance(n.func,ast.Name):
                name=n.func.id
                if name in self.functions:
                    if kw: raise PluginLimit("Helper functions take positional arguments only")
                    return self.call(name,args)
                return self.builtin(name,args,kw)
            obj=self.expr(n.func.value,env);method=n.func.attr
            if type(obj) not in (str,list,tuple,dict,set) or method not in METHODS: raise PluginLimit("Method capability denied")
            if method=='join':
                if not args or sum(len(str(x)) for x in args[0])+(max(0,len(args[0])-1)*len(obj))>MAX_TEXT:
                    raise PluginLimit("Join size limit exceeded")
            if method=='replace' and type(obj) is str and len(args)>=2:
                old,new=args[:2]
                if isinstance(old,str) and isinstance(new,str):
                    count=obj.count(old) if old else len(obj)+1
                    if len(args)>2:count=min(count,max(0,args[2]))
                    if len(obj)+count*(len(new)-len(old))>MAX_TEXT:
                        raise PluginLimit("Replacement size limit exceeded")
            fn=getattr(obj,method,None)
            if fn is None: raise PluginLimit("Method unavailable for this value")
            val=fn(*args,**kw)
            if method in ('keys','values','items'): val=list(val)
            if method=='append':bound(obj)
            return bound(val)
        raise PluginLimit(f"Unsupported expression: {type(n).__name__}")

    def builtin(self, name, args, kw):
        import builtins
        if name not in BUILTINS: raise PluginLimit("Builtin capability denied")
        if name in ('int','float') and args and isinstance(args[0],str) and len(args[0])>100:
            raise PluginLimit("Numeric input too long")
        if name in ('range','enumerate','zip'):
            if name=='range': return bound(range(*args))
            val=list(getattr(builtins,name)(*args,**kw));return bound(val)
        if name=='str' and args:bound(args[0])
        return bound(getattr(builtins,name)(*args,**kw))
