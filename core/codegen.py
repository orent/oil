"""
Sketch for oil/osh that generates and executes python code objects.

Pieces of shell code like function bodies or individual interactive 
statements are compiled to code objects by generating an AST tree
and passing them to the Python compiler.

The code objects are then executed as: exec(code, globals, locals)

This sketch uses a wrapper code object that passes control to
a Python callable. This may be gradually replaced by actual code
generation.

The locals arg to exec is a custom mapping type that implements the 
namespace semantics of shell code - including fallback to what the 
shell considers "global". This is currently a wrapper around the
state.Mem class but may be replaced by a high performance implementation 
using a dict subclass and dict.__missing__

The globals arg to exec (until Python 3.3) must be an actual python 
dict (at least until Python 3.3) and is unused for now. The code is 
assumed not to use the Python 'global' keyword and should not generate 
accesses to globals except as fallback from looking up non-existent 
locals.

"""

from types import CodeType as code

code_attrs = '''co_argcount co_nlocals co_stacksize co_flags 
    co_code co_consts co_names co_varnames co_filename co_name 
    co_firstlineno co_lnotab co_freevars co_cellvars'''.split()

def code_modify(codeobj, **kw):
    """ Return code object with modified attributes """
    args = [kw.pop(k, getattr(codeobj, k)) for k in code_attrs]
    for attr in kw: 
        getattr(codeobj, attr)  # raises AttributeError
    return code(*args)

def code_modify_consts(codeobj, mapping):
    """ Return code object with modified constants """
    co_consts = tuple(mapping.pop(c, c) for c in codeobj.co_consts)
    for m in mapping:
        raise ValueError('Unused mapping: {}'.format(m))
    return code_modify(codeobj, co_consts=co_consts)

template_code = compile("'F'('G'(),'L'())", '<wrappercode>', 'eval')
template_code = code_modify_consts(template_code, dict(G=globals, L=locals))

def wrappercode(func):
    """ Wrap a Python function in fake code object
    func(globals, locals) -> code object 
    """
    return code_modify_consts(template_code, dict(F=func))

class ShellScope(object):
    __slots__ = 'mem', 'dict'

    def __init__(self, mem):
        self.mem = mem
        self.dict = mem.var_stack[-1]

    def sanity(self):
        assert self.dict is self.mem.var_stack[-1]

    def __getitem__(self, key):
        self.sanity()
        return self.mem.GetVar(key)

    def __setitem__(self, key, value):
        self.sanity()
        return self.mem.SetVar(key, value)

    def __delitem__(self, key):
        self.sanity()
        return self.mem.Unset(key, value)
