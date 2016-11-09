# -*- coding: utf8 -*-
#
# Module LOG
#
# Part of Nutils: open source numerical utilities for Python. Jointly developed
# by HvZ Computational Engineering, TU/e Multiscale Engineering Fluid Dynamics,
# and others. More info at http://nutils.org <info@nutils.org>. (c) 2014

"""
The log module provides print methods ``debug``, ``info``, ``user``,
``warning``, and ``error``, in increasing order of priority. Output is sent to
stdout as well as to an html formatted log file if so configured.
"""

import sys, time, warnings, functools, itertools, re, abc, contextlib
from . import core

warnings.showwarning = lambda message, category, filename, lineno, *args: \
  warning( '%s: %s\n  In %s:%d' % ( category.__name__, message, filename, lineno ) )

LEVELS = 'path', 'error', 'warning', 'user', 'info', 'progress', 'debug'


## LOG

class Log( metaclass=abc.ABCMeta ):
  '''The :class:`Log` object is what is stored in the ``__log__`` property. It
  should define a ``context`` method that returns a context manager which adds
  a contextual layer and a ``write`` method.'''

  @abc.abstractmethod
  def context( self, title ):
    '''Return a context manager that adds a contextual layer named ``title``.

    .. Note:: This function is abstract.
    '''

  @abc.abstractmethod
  def write( self, level, text ):
    '''Write ``text`` with log level ``level`` to the log.

    .. Note:: This function is abstract.
    '''

class ContextLog( Log ):
  '''Base class for loggers that keep track of the current list of contexts.

  The base class implements :meth:`context` which keeps the attribute
  :attr:`_context` up-to-date.

  .. attribute:: _context

     A :class:`list` of contexts (:class:`str`\\s) that are currently active.
  '''

  def __init__( self ):
    self._context = []
    super().__init__()

  def _push_context( self, title ):
    self._context.append( title )

  def _pop_context( self ):
    self._context.pop()

  @contextlib.contextmanager
  def context( self, title ):
    '''Return a context manager that adds a contextual layer named ``title``.

    The list of currently active contexts is stored in :attr:`_context`.'''
    self._push_context( title )
    try:
      yield
    finally:
      self._pop_context()

class StdoutLog( ContextLog ):
  '''Output plain text to stream.'''

  def __init__( self, stream=sys.stdout ):
    self.stream = stream
    super().__init__()

  def _mkstr( self, level, text ):
    return ' > '.join( self._context + ([ text ] if text is not None else []) )

  def write( self, level, text, endl=True ):
    verbose = core.getprop( 'verbose', len(LEVELS) )
    if level not in LEVELS[verbose:]:
      s = self._mkstr( level, text )
      self.stream.write( s + '\n' if endl else s )

class RichOutputLog( StdoutLog ):
  '''Output rich (colored,unicode) text to stream.'''

  # color order: black, red, green, yellow, blue, purple, cyan, white

  cmap = { 'path': (2,1), 'error': (1,1), 'warning': (1,0), 'user': (3,0) }

  def _mkstr( self, level, text ):
    if text is not None:
      string = ' · '.join( self._context + [text] )
      n = len(string) - len(text)
    else:
      string = ' · '.join( self._context )
      n = len(string)
    try:
      colorid, boldid = self.cmap[level]
    except KeyError:
      return '\033[1;30m{}\033[0m{}'.format( string[:n], string[n:] )
    else:
      return '\033[1;30m{}\033[{};3{}m{}\033[0m'.format( string[:n], boldid, colorid, string[n:] )

class HtmlLog( ContextLog ):
  '''Output html nested lists.'''

  def __init__( self, htmlfile ):
    self.htmlfile = htmlfile
    super().__init__()

  @staticmethod
  def _path2href( match ):
    whitelist = ['.jpg','.png','.svg','.txt','.mp4','.webm'] + list( core.getprop( 'plot_extensions', [] ) )
    filename = match.group(0)
    ext = match.group(1)
    return ( '<a href="{0}">{0}</a>' if ext not in whitelist else '<a href="{0}" name="{0}" class="plot">{0}</a>' ).format(filename)

  def write( self, level, text ):
    if text is None:
      return
    if level == 'path':
      text = re.sub( r'\b\w+([.]\w+)\b', self._path2href, text )
    line = ' &middot; '.join( self._context + ['<span class="{}">{}</span>'.format(level,text)] )
    self.htmlfile.write( '<span class="line">{}</span>\n'.format(line) )
    self.htmlfile.flush()

class TeeLog( Log ):
  '''Simultaneously interface multiple logs'''

  def __init__( self, *logs ):
    self.logs = logs

  @contextlib.contextmanager
  def context( self, title ):
    with contextlib.ExitStack() as stack:
      for log in self.logs:
        stack.enter_context( log.context( title ) )
      yield

  def write( self, level, text ):
    for log in self.logs:
      log.write( level, text )
    
class CaptureLog( ContextLog ):
  '''Silently capture output to a string buffer while writing single character
  progress info to a secondary stream.'''

  def __init__( self, stream=sys.stdout ):
    self.stream = stream
    self.lines = []
    super().__init__()

  def write( self, level, text ):
    self.lines.append( ' > '.join( self._context + ([ text ] if text is not None else []) ) )
    self.stream.write( level[0] )
    self.stream.flush()

  @property
  def captured( self ):
    return '\n'.join( self.lines )


## INTERNAL FUNCTIONS

# references to objects that are going to be redefined
_range = range
_iter = iter
_zip = zip
_enumerate = enumerate

def _len( iterable ):
  '''Return length if available, otherwise None'''

  try:
    return len(iterable)
  except:
    return None

def _logiter( text, iterator, length=None, useitem=False ):
  dt = core.getprop( 'progress_interval', 1. )
  dtexp = core.getprop( 'progress_interval_scale', 2 )
  dtmax = core.getprop( 'progress_interval_max', 0 )
  tnext = time.time() + dt
  log = _getlog()
  for index, item in _enumerate(iterator):
    title = '%s %d' % ( text, item if useitem else index )
    if length is not None:
      title += ' ({:.0f}%)'.format( (index+.5) * 100. / length )
    with log.context( title ):
      now = time.time()
      if now > tnext:
        dt *= dtexp
        if dt > dtmax > 0:
          dt = dtmax
        log.write( 'progress', None )
        tnext = now + dt
      yield item

def _mklog():
  return RichOutputLog() if core.getprop( 'richoutput', False ) else StdoutLog()

def _getlog():
  log = core.getprop( 'log', None )
  if not isinstance( log, Log ):
    if log is not None:
      warnings.warn( '''Invalid logger object found: {!r}
        This is usually caused by manually setting the __log__ variable.'''.format(log), stacklevel=2 )
    log = _mklog()
  return log

def _path2href( match ):
  whitelist = ['.jpg','.png','.svg','.txt','.mp4','.webm'] + core.getprop( 'plot_extensions', [] )
  filename = match.group(0)
  ext = match.group(1)
  return '<a href="%s">%s</a>' % (filename,filename) if ext not in whitelist \
    else '<a href="%s" name="%s" class="plot">%s</a>' % (filename,filename,filename)

def _print( level, *args ):
  return _getlog().write( level, ' '.join( str(arg) for arg in args ) )


## MODULE-ONLY METHODS

locals().update({ name: functools.partial( _print, name ) for name in LEVELS })

def range( title, *args ):
  '''Progress logger identical to built in range'''

  items = _range( *args )
  return _logiter( title, _iter(items), length=len(items), useitem=True )

def iter( title, iterable, length=None ):
  '''Progress logger identical to built in iter'''

  return _logiter( title, _iter(iterable), length=length or _len(iterable) )

def enumerate( title, iterable, length=None ):
  '''Progress logger identical to built in enumerate'''

  return _logiter( title, _enumerate(iterable), length=length or _len(iterable) )

def zip( title, *iterables ):
  '''Progress logger identical to built in enumerate'''

  return _logiter( title, _zip(*iterables), length=None )

def count( title, start=0, step=1 ):
  '''Progress logger identical to itertools.count'''

  return _logiter( title, itertools.count(start,step), length=None, useitem=True )
    
def stack( msg, frames ):
  '''Print stack trace'''

  error( msg + '\n' + '\n'.join( str(f) for f in reversed(frames) ) )

def title( f ): # decorator
  '''Decorator, adds title argument with default value equal to the name of the
  decorated function, unless argument already exists. The title value is used
  in a static log context that is destructed with the function frame.'''

  assert getattr( f, '__self__', None ) is None, 'cannot decorate bound instance method'
  default = f.__name__
  argnames = f.__code__.co_varnames[:f.__code__.co_argcount]
  if 'title' in argnames:
    index = argnames.index( 'title' )
    if index >= len(argnames) - len(f.__defaults__ or []):
      default = f.__defaults__[ index-len(argnames) ]
    gettitle = lambda args, kwargs: args[index] if index < len(args) else kwargs.get('title',default)
  else:
    gettitle = lambda args, kwargs: kwargs.pop('title',default)
  @functools.wraps(f)
  def wrapped( *args, **kwargs ):
    __log__ = _getlog() # repeat as property for fast retrieval
    with __log__.context( gettitle(args,kwargs) ):
      return f( *args, **kwargs )
  return wrapped

def context( title ):
  return _getlog().context( title )


# vim:shiftwidth=2:softtabstop=2:expandtab:foldmethod=indent:foldnestmax=2
