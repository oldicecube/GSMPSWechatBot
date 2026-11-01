"""Interpret an allowlisted Python subset. Submitted code is never eval/exec'd."""
import ast
import json
import math
import operator
import sys


class RestrictedError(ValueError):
    pass


class Interpreter:
    LIMIT = 10000
    def __init__(self):
        self.env = {}
        self.steps = 50000
        self.depth = 0
        self.output = []
        self.output_size = 0

    def check(self, value, depth=0, budget=None):
        if budget is None:
            budget = [20000]
        budget[0] -= 1
        if budget[0] < 0 or depth > 32:
            raise RestrictedError('value nesting/size limit')
        if type(value) is int and value.bit_length() > 4096:
            raise RestrictedError('integer limit (4096 bits)')
        if type(value) is float and not math.isfinite(value):
            raise RestrictedError('non-finite number')
        if type(value) in (str, list, tuple, dict, set, range):
            if len(value) > self.LIMIT:
                raise RestrictedError('container/string limit (10000)')
            if not isinstance(value, (str, range)):
                for item in (list(value.items()) if isinstance(value, dict) else value):
                    self.check(item, depth + 1, budget)
        elif value is not None and type(value) not in (bool, int, float):
            raise RestrictedError('unsupported value type')
        return value

    def tick(self):
        self.steps -= 1
        if self.steps < 0:
            raise RestrictedError('operation limit (50000)')

    def assign(self, node, value):
        if isinstance(node, ast.Name) and not node.id.startswith('_') and node.id not in {'math','json'}:
            if node.id not in self.env and len(self.env) >= 128:
                raise RestrictedError('variable limit')
            self.env[node.id] = self.check(value)
        elif isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == len(value):
            for target, item in zip(node.elts, value):
                self.assign(target, item)
        else:
            raise RestrictedError('only simple variable assignment/unpacking is allowed')

    def binary(self, op, a, b):
        if isinstance(op, ast.Mult) and ((type(a) in (str,list,tuple) and type(b) is int) or
                                        (type(b) in (str,list,tuple) and type(a) is int)):
            seq, n = (a,b) if type(b) is int else (b,a)
            if len(seq) * max(n,0) > self.LIMIT:
                raise RestrictedError('repetition limit')
        if isinstance(op, ast.Pow):
            if type(a) not in (int,float) or type(b) not in (int,float) or abs(b) > 4096:
                raise RestrictedError('power limit')
            if type(a) is int and type(b) is int and b > 0 and abs(a).bit_length()*b > 4096:
                raise RestrictedError('power result limit')
        if isinstance(op, ast.Mod) and (type(a) not in (int,float) or type(b) not in (int,float)):
            raise RestrictedError('string formatting is disabled')
        funcs = {ast.Add:operator.add, ast.Sub:operator.sub, ast.Mult:operator.mul,
                 ast.Div:operator.truediv, ast.FloorDiv:operator.floordiv,
                 ast.Mod:operator.mod, ast.Pow:operator.pow}
        if type(op) not in funcs:
            raise RestrictedError('operator is disabled')
        return self.check(funcs[type(op)](a,b))

    def call(self, node):
        args = [self.expr(a) for a in node.args]
        if any(k.arg is None or k.arg.startswith('_') for k in node.keywords):
            raise RestrictedError('keyword expansion is disabled')
        kwargs = {k.arg:self.expr(k.value) for k in node.keywords}
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name == 'print':
                if set(kwargs) - {'sep','end'}:
                    raise RestrictedError('print only supports sep/end')
                sep, end = kwargs.get('sep',' '), kwargs.get('end','\n')
                if not isinstance(sep,str) or not isinstance(end,str):
                    raise RestrictedError('sep/end must be strings')
                text = sep.join(str(a) for a in args) + end
                self.output_size += len(text)
                if self.output_size > 8000:
                    raise RestrictedError('output limit (8000 characters)')
                self.output.append(text)
                return None
            funcs = {'abs':abs,'round':round,'len':len,'sum':sum,'min':min,'max':max,
                     'sorted':sorted,'int':int,'float':float,'str':str,'bool':bool,
                     'list':list,'tuple':tuple,'dict':dict,'set':set,'range':range,
                     'enumerate':lambda *a: list(enumerate(*a)), 'zip':lambda *a:list(zip(*a)),
                     'all':all,'any':any}
            if name not in funcs:
                raise RestrictedError('function is not allowed: '+name)
            # range is checked before it can be consumed by any allocating builtin.
            return self.check(funcs[name](*args,**kwargs))
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if isinstance(node.func.value, ast.Name) and node.func.value.id == 'math':
                allowed = {'sqrt','sin','cos','tan','asin','acos','atan','atan2','log','log10',
                           'log2','exp','floor','ceil','fabs','hypot','degrees','radians','isclose'}
                if name in allowed:
                    return self.check(getattr(math,name)(*args,**kwargs))
            if isinstance(node.func.value, ast.Name) and node.func.value.id == 'json':
                if name == 'loads' and len(args) == 1 and not kwargs and type(args[0]) is str:
                    return self.check(json.loads(args[0]))
                if name == 'dumps' and len(args) == 1 and set(kwargs) <= {'ensure_ascii','sort_keys'}:
                    return self.check(json.dumps(args[0],**kwargs))
            receiver = self.expr(node.func.value)
            if type(receiver) is str and name in {'lower','upper','strip','lstrip','rstrip','split',
                                                   'join','replace','startswith','endswith','count','find'}:
                if name == 'replace' and len(args) >= 2 and isinstance(args[1],str):
                    if (len(receiver)+1)*len(args[1])+len(receiver) > self.LIMIT:
                        raise RestrictedError('replace allocation limit')
                if name == 'join' and args:
                    self.check(args[0])
                    if sum(len(x)+len(receiver) for x in args[0]) > self.LIMIT:
                        raise RestrictedError('join allocation limit')
                return self.check(getattr(receiver,name)(*args,**kwargs))
        raise RestrictedError('attribute access/call is disabled')

    def expr(self, node):
        self.tick()
        self.depth += 1
        if self.depth > 64:
            raise RestrictedError('expression depth limit')
        try:
            return self._expr(node)
        finally:
            self.depth -= 1

    def _expr(self, node):
        if isinstance(node,ast.Constant):
            return self.check(node.value)
        if isinstance(node,ast.Name) and not node.id.startswith('_') and node.id in self.env:
            return self.env[node.id]
        if isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name) and node.value.id == 'math' and node.attr in {'pi','e','tau'}:
            return getattr(math,node.attr)
        if isinstance(node,(ast.List,ast.Tuple,ast.Set)):
            items = [self.expr(x) for x in node.elts]
            return self.check({ast.List:list,ast.Tuple:tuple,ast.Set:set}[type(node)](items))
        if isinstance(node,ast.Dict) and all(k is not None for k in node.keys):
            return self.check({self.expr(k):self.expr(v) for k,v in zip(node.keys,node.values)})
        if isinstance(node,ast.BinOp):
            return self.binary(node.op,self.expr(node.left),self.expr(node.right))
        if isinstance(node,ast.UnaryOp) and type(node.op) in {ast.Not,ast.UAdd,ast.USub}:
            return self.check({ast.Not:operator.not_,ast.UAdd:operator.pos,ast.USub:operator.neg}[type(node.op)](self.expr(node.operand)))
        if isinstance(node,ast.BoolOp):
            result = None
            for value in node.values:
                result = self.expr(value)
                if isinstance(node.op,ast.And) and not result or isinstance(node.op,ast.Or) and result:
                    break
            return result
        if isinstance(node,ast.Compare):
            a = self.expr(node.left)
            funcs = {ast.Eq:operator.eq,ast.NotEq:operator.ne,ast.Lt:operator.lt,ast.LtE:operator.le,
                     ast.Gt:operator.gt,ast.GtE:operator.ge,ast.In:lambda a,b:a in b,
                     ast.NotIn:lambda a,b:a not in b,ast.Is:operator.is_,ast.IsNot:operator.is_not}
            for op,right in zip(node.ops,node.comparators):
                b = self.expr(right)
                if not funcs[type(op)](a,b):
                    return False
                a = b
            return True
        if isinstance(node,ast.IfExp):
            return self.expr(node.body if self.expr(node.test) else node.orelse)
        if isinstance(node,ast.Subscript):
            s = node.slice
            key = slice(*(self.expr(x) if x else None for x in (s.lower,s.upper,s.step))) if isinstance(s,ast.Slice) else self.expr(s)
            return self.check(self.expr(node.value)[key])
        if isinstance(node,ast.Call):
            return self.call(node)
        if isinstance(node,ast.ListComp) and len(node.generators) == 1 and not node.generators[0].is_async:
            gen = node.generators[0]
            result = []
            for item in self.expr(gen.iter):
                self.tick()
                self.assign(gen.target,item)
                if all(self.expr(c) for c in gen.ifs):
                    result.append(self.expr(node.elt))
                    self.check(result)
            return result
        raise RestrictedError('syntax is disabled: '+type(node).__name__)

    def statements(self, nodes):
        last = None
        for node in nodes:
            self.tick()
            if isinstance(node,ast.Expr):
                last = self.expr(node.value)
            elif isinstance(node,ast.Assign):
                value = self.expr(node.value)
                for target in node.targets:
                    self.assign(target,value)
            elif isinstance(node,ast.AugAssign) and isinstance(node.target,ast.Name):
                self.assign(node.target,self.binary(node.op,self.env[node.target.id],self.expr(node.value)))
            elif isinstance(node,ast.If):
                self.statements(node.body if self.expr(node.test) else node.orelse)
            elif isinstance(node,ast.For) and not node.orelse:
                for item in self.expr(node.iter):
                    self.tick()
                    self.assign(node.target,item)
                    self.statements(node.body)
            elif isinstance(node,ast.While) and not node.orelse:
                while self.expr(node.test):
                    self.tick()
                    self.statements(node.body)
            elif not isinstance(node,ast.Pass):
                raise RestrictedError('statement is disabled: '+type(node).__name__)
        return last

    def run(self, code):
        if not isinstance(code,str) or not code.strip() or len(code) > 16000:
            raise RestrictedError('code must contain 1–16000 characters')
        tree = ast.parse(code,mode='exec')
        if sum(1 for _ in ast.walk(tree)) > 2000:
            raise RestrictedError('AST node limit')
        result = self.statements(tree.body)
        if isinstance(result,(set,tuple,range)):
            result = list(result)
        # JSON conversion validates all result types; never expose interpreter objects.
        encoded = json.dumps(result,ensure_ascii=False)
        if len(encoded) > 8000:
            raise RestrictedError('result output limit')
        return {'success':True,'stdout':''.join(self.output),'result':result}


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.buffer.read(70000).decode('utf8'))
        response = Interpreter().run(request.get('code'))
    except BaseException as exc:
        response = {'success':False,'error':f'{type(exc).__name__}: {str(exc)[:300]}'}
    sys.stdout.buffer.write(json.dumps(response,ensure_ascii=False).encode('utf8'))
