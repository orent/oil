"""
word_eval.py - Evaluator for the word language.
"""

import glob
import re
import sys

from core import braces
from core import expr_eval
from core.glob_ import Globber, GlobEscape
from core.id_kind import Id, Kind, IdName, LookupKind
from core import runtime
from core import util
from osh import ast_ as ast

arg_value_e = runtime.arg_value_e
part_value_e = runtime.part_value_e
value_e = runtime.value_e

bracket_op_e = ast.bracket_op_e
suffix_op_e = ast.suffix_op_e
word_part_e = ast.word_part_e
log = util.log
e_die = util.e_die


def _ValueToPartValue(val, quoted):
  """Helper for VarSub evaluation."""
  assert isinstance(val, runtime.value), val
  if val.tag == value_e.Undef:
    return runtime.UndefPartValue()

  elif val.tag == value_e.Str:
    return runtime.StringPartValue(val.s, not quoted, not quoted)

  elif val.tag == value_e.StrArray:
    return runtime.ArrayPartValue(val.strs)

  else:
    raise AssertionError


def _GetIfs(mem):
  """
  Used for splitting words in Splitter.
  """
  val = mem.GetVar('IFS')
  if val.tag == value_e.Undef:
    return ''
  elif val.tag == value_e.Str:
    return val.s
  else:
    # TODO: Raise proper error
    raise AssertionError("IFS shouldn't be an array")


def _GetJoinChar(mem):
  """
  For decaying arrays by joining, eg. "$@" -> $@.
  array
  """
  # https://www.gnu.org/software/bash/manual/bashref.html#Special-Parameters
  # http://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html#tag_18_05_02
  # "When the expansion occurs within a double-quoted string (see
  # Double-Quotes), it shall expand to a single field with the value of
  # each parameter separated by the first character of the IFS variable, or
  # by a <space> if IFS is unset. If IFS is set to a null string, this is
  # not equivalent to unsetting it; its first character does not exist, so
  # the parameter values are concatenated."
  val = mem.GetVar('IFS')
  if val.tag == value_e.Undef:
    return ''
  elif val.tag == value_e.Str:
    return val.s[0]
  else:
    # TODO: Raise proper error
    raise AssertionError("IFS shouldn't be an array")


def _Split(s, ifs):
  """Helper function for IFS split."""
  parts = ['']
  for c in s:
    if c in ifs:
      parts.append('')
    else:
      parts[-1] += c
  return parts


def _IfsSplit(s, ifs):
  """
  http://pubs.opengroup.org/onlinepubs/9699919799/utilities/V3_chap02.html#tag_18_06_05
  https://www.gnu.org/software/bash/manual/bashref.html#Word-Splitting

  Summary:
  1. ' \t\n' is special.  Whitespace is trimmed off the front and back.
  2. if IFS is '', no field splitting is performed.
  3. Otherwise, suppose IFS = ' ,\t'.  Then IFS whitespace is space or comma.
    a.  IFS whitespace isgnored at beginning and end.
    b. any other IFS char delimits the field, along with adjacent IFS
       whitespace.
    c. IFS whitespace shall delimit a field.

  # Can we do this be regex or something?  Use regex match?
  """
  assert isinstance(ifs, str), ifs
  if not ifs:
    return [s]  # no splitting

  # print("IFS SPLIT %r %r" % (s, ifs))
  # TODO: This detect if it's ALL whitespace?  If ifs_other is empty?
  if ifs == ' \t\n':
    return _Split(s, ifs)

  # Detect IFS whitespace
  ifs_whitespace = ''
  ifs_other = ''
  for c in ifs:
    if c in ' \t\n':
      ifs_whitespace += c
    else:
      ifs_other += c

  # TODO: Rule 3a. Ignore leading and trailing IFS whitespace?

  # hack to make an RE

  # Hm this escapes \t as \\\t?  I guess that works.
  ws_re = re.escape(ifs_whitespace)

  other_re = re.escape(ifs_other)
  #print('chars', repr(ifs_whitespace), repr(ifs_other))
  #print('RE', repr(ws_re), repr(other_re))

  # BUG: re.split() is the wrong model.  It works with the 'delimiting' model.
  # Forward iteration.  TODO: grep for IFS in dash/mksh/bash/ash.

  # ifs_ws | ifs_ws* non_ws_ifs ifs_ws*
  if ifs_whitespace and ifs_other:
    # first alternative is rule 3c.
    # BUG: It matches the whitespace first?
    pat = '[%s]+|[%s]*[%s][%s]*' % (ws_re, ws_re, other_re, ws_re)
  elif ifs_whitespace:
    pat = '[%s]+' % ws_re
  elif ifs_other:
    pat = '[%s]' % other_re
  else:
    raise AssertionError

  #print('PAT', repr(pat))
  regex = re.compile(pat)
  frags = regex.split(s)
  #log('split %r by %r -> frags %s', s, pat, frags)
  return frags


def _SplitPartsIntoFragments(part_vals, ifs):
  """
  part_value[] -> part_value[]
  Depends on no_glob
  """
  # Every part yields a single fragment array.
  frag_arrays = []
  for p in part_vals:
    if p.tag == part_value_e.StringPartValue:
      #log("SPLITTING %s with ifs %r", p, ifs)
      if p.do_split_elide:
        frags = _IfsSplit(p.s, ifs)
        res = [runtime.fragment(f, True, p.do_glob) for f in frags]
        #log("RES %s", res)
      else:
        # Example: 'a b' and "a b" don't need to be split.
        res = [runtime.fragment(p.s, p.do_split_elide, p.do_glob)]
    elif p.tag == part_value_e.ArrayPartValue:
      # "$@" and "${a[@]}" don't need to be split or globbed
      res = [runtime.fragment(f, False, False) for f in p.strs]
    else:
      raise AssertionError(p.tag)

    frag_arrays.append(res)

  return frag_arrays


def _Reframe(frag_arrays):
  """
  frag_arrays -> frag_arrays

  Example:
  [a b][c][d e] -> [a] [bcd] [e]
  """
  res = [[]]
  for frag_array in frag_arrays:
    if len(frag_array) == 0:
      res.append([])
    elif len(frag_array) == 1:
      frag = frag_array[0]
      res[-1].append(frag)
    else:
      for i, frag in enumerate(frag_array):
        if i == 0:
          res[-1].append(frag)
        else:
          res.append([frag])  # singleton frag
  return res


def _JoinElideEscape(frag_arrays, elide_empty, glob_escape):
  """Join parts without globbing or eliding.

  Returns:
    arg_value[]
  """
  args = []
  #log('_JoinElideEscape frag_arrays %s', frag_arrays)
  for frag_array in frag_arrays:
    if glob_escape:
      #log('frag_array: %s', frag_array)
      escaped_frags = []
      any_glob = False
      for frag in frag_array:
        if frag.do_glob:  # *.py should be literal
          escaped_frags.append(frag.s)
          any_glob = True
        else:
          # "*.py" should be glob-escaped to \*.py
          escaped_frags.append(GlobEscape(frag.s))

      arg_str = ''.join(escaped_frags)
      #log('ARG STR %s', arg_str)
      if any_glob:
        arg = runtime.GlobArg(arg_str)
      else:
        # e.g. 'foo'"${var}" shouldn't be globbed
        # TODO: combine with below:
        arg = runtime.ConstArg(''.join(frag.s for frag in frag_array))
    else:
      arg = runtime.ConstArg(''.join(frag.s for frag in frag_array))

    # Elide $a$b, but not $a"$b" or $a''
    if (elide_empty and
        not arg.s and all(frag.do_elide for frag in frag_array)):
      #log('eliding frag_array %s', frag_array)
      continue

    args.append(arg)

  return args


def _DecayPartValuesToString(part_vals, join_char):
  # Decay ${a=x"$@"x} to string.
  out = []
  for p in part_vals:
    if p.tag == part_value_e.StringPartValue:
       out.append(p.s)
    else:
      last = len(p.strs) - 1
      for i, s in enumerate(p.strs):
        out.append(s)
        if i != last:
          out.append(join_char)
  return ''.join(out)


# Eval is for ${a-} and ${a+}, Error is for ${a?}, and Assign is for ${a=}

Effect = util.Enum('Effect', 'SpliceParts Error SpliceAndAssign NoOp'.split())


class _WordPartEvaluator:
  """Abstract base class."""

  def __init__(self, mem, exec_opts, word_ev):
    self.mem = mem  # for $HOME, $1, etc.
    self.exec_opts = exec_opts  # for nounset
    self.word_ev = word_ev  # for arith words, var op words
    # NOTE: Executor also instantiates one.
    self.arith_ev = expr_eval.ArithEvaluator(mem, exec_opts, word_ev)

  def _EvalCommandSub(self, part, quoted):
    """Abstract since it has a side effect.

    Args:
      part: CommandSubPart

    Returns:
       part_value
    """
    raise NotImplementedError

  def _EvalTildeSub(self, prefix):
    """Evaluates ~ and ~user.

    Args:
      prefix: The tilde prefix (possibly empty)
    """
    if prefix == '':
      # First look up the HOME var, and then env var
      val = self.mem.GetVar('HOME')
      if val.tag == value_e.Str:
        return val.s
      elif val.tag == value_e.StrArray:
        raise AssertionError

      s = util.GetHomeDir()
      if s is None:
        s = '~' + prefix  # No expansion I guess
      return s

    # http://linux.die.net/man/3/getpwnam
    try:
      e = pwd.getpwnam(prefix)
    except KeyError:
      s = '~' + prefix
    else:
      s = e.pw_dir
    return s

  def _EvalVarNum(self, var_num):
    assert var_num >= 0
    return self.mem.GetArgNum(var_num)

  def _EvalSpecialVar(self, op_id, quoted):
    # $@ is special -- it need to know whether it is in a double quoted
    # context.
    #
    # - If it's $@ in a double quoted context, return an ARRAY.
    # - If it's $@ in a normal context, return a STRING, which then will be
    # subject to splitting.

    if op_id in (Id.VSub_At, Id.VSub_Star):
      argv = self.mem.GetArgv()
      val = runtime.StrArray(argv)
      if op_id == Id.VSub_At:
        # "$@" evaluates to an array, $@ should be decayed
        return val, not quoted
      else:  # $@ $* "$*"
        return val, True

    elif op_id == Id.VSub_Hyphen:
      s = self.exec_opts.GetDollarHyphen()
      return runtime.Str(s), False
    else:
      val = self.mem.GetSpecialVar(op_id)
      return val, False  # dont' decay

  def _ApplyTestOp(self, val, op, quoted):
    """
    Returns:
      part_vals, Effect

      ${a:-} returns part_value[]
      ${a:+} returns part_value[]
      ${a:?error} returns error word?
      ${a:=} returns part_value[] but also needs self.mem for side effects.

      So I guess it should return part_value[], and then a flag for raising an
      error, and then a flag for assigning it?
      The original BracedVarSub will have the name.

    Example of needing multiple part_value[]

      echo X-${a:-'def'"ault"}-X

    We return two part values from the BracedVarSub.  Also consider:

      echo ${a:-x"$@"x}
    """
    undefined = (val.tag == value_e.Undef)

    # TODO: Change this to a bitwise test?
    if op.op_id in (
        Id.VTest_ColonHyphen, Id.VTest_ColonEquals, Id.VTest_ColonQMark,
        Id.VTest_ColonPlus):
      is_falsey = (
          undefined or
          (val.tag == value_e.Str and not val.s) or
          (val.tag == value_e.StrArray and not val.strs)
      )
    else:
      is_falsey = undefined

    #print('!!',id, is_falsey)
    if op.op_id in (Id.VTest_ColonHyphen, Id.VTest_Hyphen):
      if is_falsey:
        part_vals = self.word_ev._EvalParts(op.arg_word, quoted=quoted)
        return part_vals, Effect.SpliceParts
      else:
        return None, Effect.NoOp

    elif op.op_id in (Id.VTest_ColonPlus, Id.VTest_Plus):
      # Inverse of the above.
      if is_falsey:
        return None, Effect.NoOp
      else:
        part_vals = self.word_ev._EvalParts(op.arg_word, quoted=quoted)
        return part_vals, Effect.SpliceParts

    elif op.op_id in (Id.VTest_ColonEquals, Id.VTest_Equals):
      if is_falsey:
        part_vals = self.word_ev._EvalParts(op.arg_word, quoted=quoted)
        return part_vals, Effect.SpliceAndAssign
      else:
        return None, Effect.NoOp

    elif op.op_id in (Id.VTest_ColonQMark, Id.VTest_QMark):
      # TODO: Construct error
      # So the test fails!  Exit code 1 makes it pass.
      sys.exit(33)
      raise NotImplementedError

    # TODO:
    # +  -- inverted test -- assign to default
    # ?  -- error
    # =  -- side effect assignment
    else:
      raise NotImplementedError(id)

  def _ApplyPrefixOp(self, val, op_id):
    """
    Returns:
      value
    """
    assert val.tag != value_e.Undef

    if op_id == Id.VSub_Pound:  # LENGTH
      if val.tag == value_e.Str:
        length = len(val.s)
      elif val.tag == value_e.StrArray:
        # TODO: There can be empty placeholder values in the array.
        length = len(val.strs)
      return runtime.Str(str(length))
    else:
      raise NotImplementedError(op_id)

  def _ApplyOtherSuffixOp(self, val, op):

    # NOTES:
    # - These are VECTORIZED on arrays
    #   - I want to allow this with my shell dialect: @{files|slice 1
    #   2|upper} does the same thing to all of them.
    # - How to do longest and shortest possible match?  bash and mksh both
    #   call fnmatch() in a loop, with possible short-circuit optimizations.
    #   - TODO: Write a test program to show quadratic behavior?
    #   - original AT&T ksh has special glob routines that returned the match
    #   positions.
    #   Implementation:
    #   - Test if it is a LITERAL or a Glob.  Then do a plain string
    #   operation.
    #   - If it's a glob, do the quadratic algorithm.
    #   - NOTE: bash also has an optimization where it extracts the LITERAL
    #   parts of the string, and does a prematch.  If none of them match,
    #   then it SKIPs the quadratic algorithm.
    #   - The real solution is to compile a glob to RE2, but I want to avoid
    #   that dependency right now... libc regex is good for a bunch of
    #   things.
    # - Bash has WIDE CHAR support for this.  With wchar_t.
    #   - All sorts of functions like xdupmbstowcs
    #
    # And then pat_subst() does some special cases.  Geez.

    assert val.tag != value_e.Undef

    op_kind = LookupKind(op.op_id)

    new_val = None

    if op_kind == Kind.VOp1:
      #log('%s', op)
      arg_val = self.word_ev.EvalWordToString(op.arg_word, do_fnmatch=True)
      assert arg_val.tag == value_e.Str

      looks_like_glob = False
      if looks_like_glob:
        if op.op_id == Id.VOp1_Pound:  # shortest prefix
          raise NotImplementedError
        elif op.op_id == Id.VOp1_DPound:  # longest prefix
          raise NotImplementedError

        elif op.op_id == Id.VOp1_Percent:  # shortest suffix
          raise NotImplementedError
        elif op.op_id == Id.VOp1_DPercent:  # longest suffix
          raise NotImplementedError
        else:
          raise AssertionError(op.op_id)

      else:
        op_str = arg_val.s

        # TODO: Factor these out into a common fuction?
        if op.op_id in (Id.VOp1_Pound, Id.VOp1_DPound):  # const prefix
          prefix = op_str

          if val.tag == value_e.Str:
            if val.s.startswith(prefix):
              # Mutate it so we preserve the flags.
              new_val = runtime.Str(val.s[len(prefix):])
            else:
              #log("Str: %r doesn't end with %r", val.s, suffix)
              pass

          elif val.tag == value_e.StrArray:
            new_val = runtime.StrArray()
            for i, s in enumerate(val.strs):
              if s.startswith(prefix):
                # Mutate it so we preserve the flags.
                new_s = s[len(prefix):]
                #log('%s -> %s', s, s[:-len(suffix)])
              else:
                new_s = s
                #log("Array: %r doesn't end with %r", s, suffix)
              new_val.strs.append(new_s)

        elif op.op_id in (Id.VOp1_Percent, Id.VOp1_DPercent):  # const suffix
          suffix = op_str

          if val.tag == value_e.Str:
            if val.s.endswith(suffix):
              # Mutate it so we preserve the flags.
              new_val = runtime.Str(val.s[:-len(suffix)])
            else:
              #log("Str: %r doesn't end with %r", val.s, suffix)
              pass

          elif val.tag == value_e.StrArray:
            new_val = runtime.StrArray()
            for i, s in enumerate(val.strs):
              if s.endswith(suffix):
                # Mutate it so we preserve the flags.
                new_s = s[:-len(suffix)]
                #log('%s -> %s', s, s[:-len(suffix)])
              else:
                new_s = s
                #log("Array: %r doesn't end with %r", s, suffix)
              new_val.strs.append(new_s)

        else:
          raise AssertionError(op.op_id)

    elif op_kind == Kind.VOp2:
      if op.op_id == Id.VOp2_Slash:  # PatSub, vectorized
        raise NotImplementedError

      # Either string slicing or array slicing.  However string slicing has a
      # unicode problem?  TODO: Test bash out.  We need utf-8 parsing in C++?
      #
      # Or maybe have a different operator for byte slice and char slice.
      elif op.op_id == Id.VOp2_Colon:
        raise NotImplementedError

    else:
      raise NotImplementedError(op)

    if new_val:
      return new_val
    else:
      return val

  def _EvalDoubleQuotedPart(self, part):
    # Example of returning array:
    # $ a=(1 2); b=(3); $ c=(4 5)
    # $ argv "${a[@]}${b[@]}${c[@]}"
    # ['1', '234', '5']
    # Example of multiple parts
    # $ argv "${a[@]}${undef[@]:-${c[@]}}"
    # ['1', '24', '5']

    #log('DQ part %s', part)

    # Special case for "".  The parser outputs (DoubleQuotedPart []), instead
    # of (DoubleQuotedPart [LiteralPart '']).  This is better but it means we
    # have to check for it.
    if not part.parts:
      return runtime.StringPartValue('', False, False)

    frag_arrays = [[]]
    for p in part.parts:
      for part_val in self._EvalWordPart(p, quoted=True):
        assert isinstance(part_val, runtime.part_value), (p, part_val)
        if part_val.tag == part_value_e.StringPartValue:
          frag_arrays[-1].append(part_val.s)
        else:
          for i, s in enumerate(part_val.strs):
            if i == 0:
              frag_arrays[-1].append(s)
            else:
              frag_arrays.append([s])

    #log('frag_arrays %s', frag_arrays)

    strs = []
    for frag_array in frag_arrays:
      # "${empty[@]}" leads to [[]], should eval to [] and not ['']
      if frag_array:
        strs.append(''.join(frag_array))

    # This should be able to evaluate to EMPTY ARRAY!
    #log('strs %s', strs)
    if len(strs) == 1:
      val = runtime.StringPartValue(strs[0], False, False)
    else:
      val = runtime.ArrayPartValue(strs)
    return val

  def _DecayArray(self, val):
    sep = _GetJoinChar(self.mem)
    assert val.tag == value_e.StrArray, val
    return runtime.Str(sep.join(val.strs))

  def _EmptyStrOrError(self, val, token=None):
    assert isinstance(val, runtime.value), val

    if val.tag == value_e.Undef:
      if self.exec_opts.nounset:
        if token is None:
          e_die('Undefined variable')
        else:
          name = token.val[1:] if token.val.startswith('$') else token.val
          e_die('Undefined variable %r', name, token=token)
      else:
        return runtime.Str('')
    else:
      return val

  def _EmptyStrArrayOrError(self, token):
    assert token is not None
    if self.exec_opts.nounset:
      e_die('Undefined array %r', token.val, token=token)
    else:
      return runtime.StrArray([])

  def _EvalBracedVarSub(self, part, quoted):
    """
    Returns:
      part_value[]
    """
    # We have four types of operator that interact.
    #
    # 1. Bracket: value -> (value, bool decay_array)
    #
    # 2. Then these four cases are mutually exclusive:
    #
    #   a. Prefix length: value -> value
    #   b. Test: value -> part_value[]
    #   c. Other Suffix: value -> value
    #   d. no operator: you have a value
    #
    # That is, we don't have both prefix and suffix operators.
    #
    # 3. Process decay_array here before returning.

    decay_array = False  # for $*, ${a[*]}, etc.

    var_name = None  # For ${foo=default}

    # 1. Evaluate from (var_name, var_num, token Id) -> value
    if part.token.id == Id.VSub_Name:
      var_name = part.token.val
      val = self.mem.GetVar(var_name)
      #log('EVAL NAME %s -> %s', var_name, val)

    elif part.token.id == Id.VSub_Number:
      var_num = int(part.token.val)
      val = self._EvalVarNum(var_num)
    else:
      # $* decays
      val, decay_array = self._EvalSpecialVar(part.token.id, quoted)

    # 2. Bracket: value -> (value v, bool decay_array)
    # decay_array is for joining ${a[*]} and unquoted ${a[@]} AFTER suffix ops
    # are applied.  If we take the length with a prefix op, the distinction is
    # ignored.
    if part.bracket_op:
      if part.bracket_op.tag == bracket_op_e.WholeArray:
        op_id = part.bracket_op.op_id

        if op_id == Id.Lit_At:
          if not quoted:
            decay_array = True  # ${a[@]} decays but "${a[@]}" doesn't
          if val.tag == value_e.Undef:
            val = self._EmptyStrArrayOrError(part.token)
          elif val.tag == value_e.Str:
            raise RuntimeError("Can't index string with @")
          elif val.tag == value_e.StrArray:
            val = runtime.StrArray(val.strs)

        elif op_id == Id.Arith_Star:
          decay_array = True  # both ${a[*]} and "${a[*]}" decay
          if val.tag == value_e.Undef:
            val = self._EmptyStrArrayOrError(part.token)
          elif val.tag == value_e.Str:
            raise RuntimeError("Can't index string with *")
          elif val.tag == value_e.StrArray:
            # Always decay_array with ${a[*]} or "${a[*]}"
            val = runtime.StrArray(val.strs)

        else:
          raise AssertionError(op_id)  # unknown

      elif part.bracket_op.tag == bracket_op_e.ArrayIndex:
        anode = part.bracket_op.expr
        index = self.arith_ev.Eval(anode)

        if val.tag == value_e.Undef:
          pass  # it will be checked later
        elif val.tag == value_e.Str:
          # TODO: Implement this as an extension. Requires unicode support.
          # Bash treats it as an array.
          e_die("Can't index string %r with integer", part.token.val)
        elif val.tag == value_e.StrArray:
          try:
            s = val.strs[index]
          except IndexError:
            val = runtime.Undef()
          else:
            val = runtime.Str(s)

      else:
        raise AssertionError(part.bracket_op.tag)

    if part.prefix_op:
      val = self._EmptyStrOrError(val)  # maybe error
      val = self._ApplyPrefixOp(val, part.prefix_op)
      # At least for length, we can't have a test or suffix afterward.

    elif part.suffix_op:
      out_part_vals = []
      op = part.suffix_op
      if op.tag == suffix_op_e.StringUnary:
        if LookupKind(part.suffix_op.op_id) == Kind.VTest:
          # VTest: value -> part_value[]
          new_part_vals, effect = self._ApplyTestOp(val, part.suffix_op,
                                                    quoted)

          # NOTE: Splicing part_values is necessary because of code like
          # ${undef:-'a b' c 'd # e'}.  Each part_value can have a different
          # do_glob/do_elide setting.
          if effect == Effect.SpliceParts:
            return new_part_vals  # EARLY RETURN

          elif effect == Effect.SpliceAndAssign:
            if var_name is None:
              # TODO: error context
              e_die("Can't assign to special variable")
            else:
              # NOTE: This decays arrays too!  'set -o strict_array' could
              # avoid it.
              rhs_str = _DecayPartValuesToString(new_part_vals,
                                                 _GetJoinChar(self.mem))
              self.mem.SetLocalString(var_name, rhs_str)
            return new_part_vals

          elif effect == Effect.Error:
            raise NotImplementedError

          else:
            # The old one
            #val = self._EmptyStringPartOrError(part_val, quoted)
            #out_part_vals.append(part_val)
            pass  # do nothing, may still be undefined

        else:
          val = self._EmptyStrOrError(val)  # maybe error
          # Other suffix: value -> value
          val = self._ApplyOtherSuffixOp(val, part.suffix_op)

      elif op.tag == suffix_op_e.PatSub:
        raise NotImplementedError(op)

      elif op.tag == suffix_op_e.Slice:
        raise NotImplementedError(op)

    # After applying suffixes, process decay_array here.
    if decay_array:
      val = self._DecayArray(val)

    # No prefix or suffix ops
    val = self._EmptyStrOrError(val)

    return [_ValueToPartValue(val, quoted)]

  def _EvalWordPart(self, part, quoted=False):
    """Evaluate a word part.

    Returns:
      A LIST of part_value, rather than just a single part_value, because of
      the quirk where ${a:-'x'y} is a single WordPart, but yields two
      part_values.
    """
    if part.tag == word_part_e.ArrayLiteralPart:
      raise AssertionError(
          'Array literal should have been handled at word level')

    elif part.tag == word_part_e.LiteralPart:
      s = part.token.val
      do_split_elide = not quoted
      do_glob = True
      return [runtime.StringPartValue(s, do_split_elide, do_glob)]

    elif part.tag == word_part_e.EscapedLiteralPart:
      val = part.token.val
      assert len(val) == 2, val  # e.g. \*
      assert val[0] == '\\'
      s = val[1]
      return [runtime.StringPartValue(s, False, False)]

    elif part.tag == word_part_e.SingleQuotedPart:
      s = ''.join(t.val for t in part.tokens)
      return [runtime.StringPartValue(s, False, False)]

    elif part.tag == word_part_e.DoubleQuotedPart:
      return [self._EvalDoubleQuotedPart(part)]

    elif part.tag == word_part_e.CommandSubPart:
      if part.left_token.id not in (Id.Left_CommandSub, Id.Left_Backtick):
        # TODO: If token is Id.Left_ProcSubIn or Id.Left_ProcSubOut, we have to
        # supply something like /dev/fd/63.
        raise NotImplementedError(part.left_token.id)

      return [self._EvalCommandSub(part.command_list, quoted)]

    elif part.tag == word_part_e.SimpleVarSub:
      decay_array = False
      # 1. Evaluate from (var_name, var_num, token) -> defined, value
      if part.token.id == Id.VSub_Name:
        var_name = part.token.val[1:]
        val = self.mem.GetVar(var_name)
      elif part.token.id == Id.VSub_Number:
        var_num = int(part.token.val[1:])
        val = self._EvalVarNum(var_num)
      else:
        val, decay_array = self._EvalSpecialVar(part.token.id, quoted)

      #log('SIMPLE %s', part)
      val = self._EmptyStrOrError(val, token=part.token)
      if decay_array:
        val = self._DecayArray(val)
      part_val = _ValueToPartValue(val, quoted)
      return [part_val]

    elif part.tag == word_part_e.BracedVarSub:
      return self._EvalBracedVarSub(part, quoted)

    elif part.tag == word_part_e.TildeSubPart:
      # We never parse a quoted string into a TildeSubPart.
      assert not quoted
      s = self._EvalTildeSub(part.prefix)
      return [runtime.StringPartValue(s, False, False)]

    elif part.tag == word_part_e.ArithSubPart:
      num = self.arith_ev.Eval(part.anode)
      return [runtime.StringPartValue(str(num), True, True)]

    else:
      raise AssertionError(part.tag)


class _WordEvaluator:
  """Abstract base class for word evaluators.

  Public entry points:
    EvalWordToString
    EvalWordToAny
    EvalWordSequence
    Error
  """
  def __init__(self, mem, exec_opts, part_ev):
    self.mem = mem
    self.exec_opts = exec_opts

    self.part_ev = part_ev
    self.globber = Globber(exec_opts)

  def _EvalParts(self, word, quoted=False):
    """Helper for EvalWordToAny and _EvalWordAndReframe.

    Returns part_value[]."""
    assert isinstance(word, ast.CompoundWord), \
        "Expected CompoundWord, got %s" % word

    part_vals = []
    for p in word.parts:
      for v in self.part_ev._EvalWordPart(p, quoted=quoted):
        #log('_EvalParts %s -> %s (q=%s)', p, v, quoted)
        part_vals.append(v)
    return part_vals

  def EvalWordToString(self, word, do_fnmatch=False, decay=False):
    """
    Used for redirect arg, ControlFlow arg, ArithWord, BoolWord, etc.

    do_fnmatch is true for case $pat and RHS of [[ == ]].

    pat="*.py"
    case $x in
      $pat) echo 'matches glob pattern' ;;
      "$pat") echo 'equal to glob string' ;;  // must be glob escaped
    esac
    """
    strs = []
    for part in word.parts:
      for part_val in self.part_ev._EvalWordPart(part, quoted=False):
        # TODO: if decay, then allow string part.  e.g. for here word or here
        # doc with "$@".

        if part_val.tag != part_value_e.StringPartValue:
          # Example: echo f > "$@".  TODO: Add proper context.  
          e_die("Expected string, got %s", part)
        if do_fnmatch:
          if part_val.do_glob:
            strs.append(part_val.s)
          else:
            strs.append(GlobEscape(part_val.s))
        else:
          strs.append(part_val.s)

    return runtime.Str(''.join(strs))

  def EvalWordToAny(self, word, glob_escape=False):
    """
    Used for RHS of assignment

    Also used for default value?  e.g. "${a:-"a" "b"}" and so forth.

    Returns:
      arg_value
      Or maybe just string?  Whatever would go in ConstArg and GlobArg.
      But you don't need to distinguish it later.
      You could also have EvalWord and EvalGlobWord methods or EvalPatternWord
    """
    # Special case for a=(1 2).  ArrayLiteralPart won't appear in words that
    # don't look like assignments.
    if (len(word.parts) == 1 and
        word.parts[0].tag == word_part_e.ArrayLiteralPart):
      array_words = word.parts[0].words
      words = braces.BraceExpandWords(array_words)
      strs = self._EvalWordSequence(words)
      #log('ARRAY LITERAL EVALUATED TO -> %s', strs)
      return runtime.StrArray(strs)

    part_vals = self._EvalParts(word)
    #log('part_vals %s', part_vals)

    # Instead of splitting, do a trivial transformation to frag array.
    # Example:
    # foo="-$@-${a[@]}-" requires fragment, reframe, and simple join
    frag_arrays = []
    for p in part_vals:
      if p.tag == part_value_e.StringPartValue:
        frag_arrays.append([runtime.fragment(p.s, False, False)])
      elif p.tag == part_value_e.ArrayPartValue:
        frag_arrays.append(
            [runtime.fragment(s, False, False) for s in p.strs])
      else:
        raise AssertionError

    frag_arrays = _Reframe(frag_arrays)
    #log('frag_arrays %s', frag_arrays)

    # Simple join
    args = []
    for frag_array in frag_arrays:
      args.append(''.join(frag.s for frag in frag_array))

    # Example:
    # a=(1 2)
    # b=$a  # one word
    # c="${a[@]}"  # two words
    if len(args) == 1:
      val = runtime.Str(args[0])
    else:
      # NOTE: For bash compatibility, could have an option to join them here.
      # foo="-$@-${a[@]}-"  -- join with IFS again, like "$*" ?
      # Or maybe do that in cmd_exec in assignment.
      val = runtime.StrArray(args)

    return val

  def _EvalWordAndReframe(self, word):
    """Helper for _EvalWordSequence.

    Used in command, array, and foreach context.

    Args:
      word: CompoundWord
    Returns:
      val: runtime.value
    """
    part_vals = self._EvalParts(word)
    #log('part_vals after _EvalParts %s', part_vals)
    ifs = _GetIfs(self.mem)
    frag_arrays = _SplitPartsIntoFragments(part_vals, ifs)
    #log('Fragments after split: %s', frag_arrays)
    frag_arrays = _Reframe(frag_arrays)
    #log('Fragments after reframe: %s', frag_arrays)

    glob_escape = not self.exec_opts.noglob

    # NOTE: Empirically, elision depends on IFS.  I don't see it in the POSIX
    # spec though.  This may need to be revised to have ' \t\n'.
    elide_empty = True
    for c in ifs:
      if c not in ' \t\n':
        elide_empty = False

    args = _JoinElideEscape(frag_arrays, elide_empty, glob_escape)
    #log('After _JoinElideEscape %s', args)
    return args

  def _EvalWordSequence(self, words):
    """Turns a list of Words into a list of strings.

    Unlike the EvalWord*() methods, it does globbing.

    Args:
      words: list of Word instances

    Returns:
      argv: list of string arguments, or None if there was an eval error
    """
    # Parse time:
    # 1. brace expansion.  TODO: Do at parse time.
    # 2. Tilde detection.  DONE at parse time.  Only if Id.Lit_Tilde is the
    # first WordPart.
    #
    # Run time:
    # 3. tilde sub, var sub, command sub, arith sub.  These are all
    # "concurrent" on WordParts.  (optional process sub with <() )
    # 4. word splitting.  Can turn this off with a shell option?  Definitely
    # off for oil.
    # 5. globbing -- several exec_opts affect this: nullglob, safeglob, etc.

    #log('W %s', words)
    argv = []
    for w in words:
      args = self._EvalWordAndReframe(w)
      #log('A %s', args)
      for arg in args:
        if arg.tag == arg_value_e.ConstArg:
          argv.append(arg.s)
        elif arg.tag == arg_value_e.GlobArg:
          results = self.globber.Expand(arg.s)
          #log('Glob Expand %s to %s', arg.s, results)
          argv.extend(results)
        else:
          raise AssertionError(arg.tag)

    #log('ARGV %s', argv)
    return argv

  def EvalWordSequence(self, words):
    """
    Used in: SimpleCommand, ForEach.
    """
    # TODO: Remove this stub
    return self._EvalWordSequence(words)


class _NormalPartEvaluator(_WordPartEvaluator):
  """The Executor uses this to evaluate words."""

  def __init__(self, mem, exec_opts, ex, word_ev):
    _WordPartEvaluator.__init__(self, mem, exec_opts, word_ev)
    self.ex = ex

  def _EvalCommandSub(self, node, quoted):
    stdout = self.ex.RunCommandSub(node)

    # Runtime errors test case: # $("echo foo > $@")

    # Why rstrip()?
    # https://unix.stackexchange.com/questions/17747/why-does-shell-command-substitution-gobble-up-a-trailing-newline-char
    return runtime.StringPartValue(stdout, not quoted, not quoted)


class NormalWordEvaluator(_WordEvaluator):

  def __init__(self, mem, exec_opts, ex):
    part_ev = _NormalPartEvaluator(mem, exec_opts, ex, self)
    _WordEvaluator.__init__(self, mem, exec_opts, part_ev)


class _CompletionPartEvaluator(_WordPartEvaluator):
  """For completion.

  We are disabling command substitution for now.

  TODO: Also disable side effects!  Like ${a:=b} rather than ${a:-b}
  And also $(( a+=1 ))

  TODO: Unify with static_eval?  Completion allows more stuff like var names,
  and maybe words within arrays as well.
  """
  def __init__(self, mem, exec_opts, word_ev):
    _WordPartEvaluator.__init__(self, mem, exec_opts, word_ev)

  def _EvalCommandSub(self, node, quoted):
    # Just  return a dummy string?
    return runtime.StringPartValue(
        '__COMMAND_SUB_NOT_EXECUTED__', not quoted, not quoted)


class CompletionWordEvaluator(_WordEvaluator):

  def __init__(self, mem, exec_opts):
    part_ev = _CompletionPartEvaluator(mem, exec_opts, self)
    _WordEvaluator.__init__(self, mem, exec_opts, part_ev)
