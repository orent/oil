#!/usr/bin/python
"""
args.py

All shells have their own flag parsing, so I guses we need our own too.

Differences with getopt/optparse:

- accepts +o +n for 'set' and bin/osh
  - pushd and popd also uses +, although it's not an arg.
- parses args -- well argparse is supposed to do this
- maybe: integrate with usage
- maybe: integrate with flags

- Actually I think optparse does the same thing?
  - maybe just fork it?
  - I don't like the store_true crap

issue:
  - does it support long args
    - --foo bar
    - --foo=bar
    - ABBREVIATIONS for long args
  - does it abort after arg
    - echo -n foo -e   #  GNU allows, but bash doesn't

optparse: 
  - has option groups

- notes about builtins:
  - eval implicitly joins it args, we don't want to do that
    - how about strict-builtin-syntax ?


Usage:

spec = arg.Spec()
args = spec.parse(argv)

  args.names
  args.rest
  args.n
  args.v

  opts.v
  opts.args


special cases:
  echo

NOTE: 
- bash is inconsistent about checking for extra args
  - exit 1 2 complains, but pushd /lib /bin just ignores second argument
  - it has a no_args() function that isn't called everywhere.  It's not
    declarative.

  #
  # Is it nicer to do:
  #
  # opt, opt_index = my_getopt(argv, 'n')
  #
  # opt.n, opt.f
  # opt.no_newline, opts.c_escape
  #
  # FLAG_n
  # OPT.n
  # FLAG.n
  #
  # flag, opt_index = my_getopt
  # flag.n
  #
  # if flag.n:

  # NOTE: getopt uses a quadratic algorithm!  It calls args[1:] repeatedly.
  # I guess use this interface for now; rewrite it later.
  # is optparse better?  It's much longer.  getopt is only 210 lines.
  # optparse is 1704.  It generates help though.
"""

import sys

class UsageError(Exception):
  """Raised by builtins upon flag parsing error."""
  pass


def main(argv):
  print 'Hello from args.py'


if __name__ == '__main__':
  try:
    main(sys.argv)
  except RuntimeError as e:
    print >>sys.stderr, 'FATAL: %s' % e
    sys.exit(1)
