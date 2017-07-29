# -*- coding: utf8 -*-
#
# Module MODEL
#
# Part of Nutils: open source numerical utilities for Python. Jointly developed
# by HvZ Computational Engineering, TU/e Multiscale Engineering Fluid Dynamics,
# and others. More info at http://nutils.org <info@nutils.org>. (c) 2014

"""
The solver module defines the :class:`Integral` class, which represents an
unevaluated integral. This is useful for fully automated solution procedures
such as Newton, that require functional derivatives of an entire functional.

To demonstrate this consider the following setup:

>>> from nutils import mesh, function, solver
>>> ns = function.Namespace()
>>> domain, ns.x = mesh.rectilinear( [4,4] )
>>> ns.basis = domain.basis( 'spline', degree=2 )
>>> cons = domain.boundary['left,top'].project( 0, onto=ns.basis, geometry=ns.x, ischeme='gauss4' )
project > constrained 11/36 dofs, error 0.00e+00/area
>>> ns.u = 'basis_n ?lhs_n'

Function ``u`` represents an element from the discrete space but cannot not
evaluated yet as we did not yet establish values for ``?lhs``. It can,
however, be used to construct a residual functional ``res``. Aiming to solve
the Poisson problem ``u_,kk = f`` we define the residual functional ``res = v,k
u,k + v f`` and solve for ``res == 0`` using ``solve_linear``:

>>> res = domain.integral('basis_n,i u_,i + basis_n' @ ns, geometry=ns.x, degree=2)
>>> lhs = solver.solve_linear( 'lhs', residual=res, constrain=cons )
solving system > solving system using sparse direct solver

The coefficients ``lhs`` represent the solution to the Poisson problem.

In addition to ``solve_linear`` the solver module defines ``newton`` and
``pseudotime`` for solving nonlinear problems, as well as ``impliciteuler`` for
time dependent problems.
"""

from . import function, cache, log, util, numeric
import numpy, itertools, functools, numbers, collections


def _find_argument( name, *funcs ):
  '''find and return a :class:`nutils.function.Argument` with ``name`` in ``funcs``'''

  class Found( Exception ):
    def __init__( self, target ):
      self.target = target
      super().__init__( 'found' )

  def find( value ):
    if isinstance( value, function.Argument ) and value._name == name:
      raise Found( value )
    return function.edit( value, find )

  funcs_ = []
  for func in funcs:
    if isinstance( func, Integral ):
      funcs_.extend(func.values())
    else:
      funcs_.append( func )

  try:
    for func in funcs_:
      function.edit( func, find )
  except Found as e:
    return e.target
  raise ValueError( 'target {!r} not found'.format( name ) )


class Integral( dict ):
  '''Postponed integral, used for derivative purposes'''

  def __init__(self, integrand, domain, ischeme):
    self[cache.HashableAny(domain), ischeme] = integrand
    self.shape = integrand.shape

  @classmethod
  def empty( self, shape ):
    empty = dict.__new__( Integral )
    empty.shape = tuple(shape)
    return empty

  @classmethod
  def concatenate( cls, integrals ):
    assert all( integral.shape[1:] == integrals[0].shape[1:] for integral in integrals[1:] )
    concatenate = cls.empty( ( sum( integral.shape[0] for integral in integrals ), ) + integrals[0].shape[1:] )
    for domain_ischeme in {di for integral in integrals for di in integral}:
      integrands = [ integral.get(domain_ischeme, function.zeros(integral.shape)) for integral in integrals ]
      concatenate[domain_ischeme] = function.concatenate( integrands, axis=0 )
    return concatenate

  @classmethod
  def multieval( cls, *integrals, fcache=None, arguments=None ):
    if fcache is None:
      fcache = cache.WrapperCache()
    assert all( isinstance( integral, cls ) for integral in integrals )
    retvals = []
    for domain_ischeme in {di for integral in integrals for di in integral}:
      integrands = [ integral.get(domain_ischeme, function.zeros(integral.shape)) for integral in integrals ]
      domain, ischeme = domain_ischeme
      retvals.append( domain.obj.integrate( integrands, ischeme=ischeme, fcache=fcache, arguments=arguments ) )
    return numpy.sum( retvals, axis=0 )

  def eval( self, *, fcache=None, arguments=None ):
    if fcache is None:
      fcache = cache.WrapperCache()
    values = [ domain.obj.integrate( integrand, ischeme=ischeme, fcache=fcache, arguments=arguments ) for (domain, ischeme), integrand in self.items() ]
    return numpy.sum( values, axis=0 )

  def derivative(self, target):
    target = _find_argument(target, self)
    assert target.ndim == 1
    seen = {}
    derivative = self.empty(self.shape+target.shape)
    for domain_ischeme, integrand in self.items():
      derivative[domain_ischeme] = function.derivative(integrand, var=target, seen=seen)
    return derivative

  def replace( self, arguments ):
    replace = self.empty( self.shape )
    for domain_ischeme, integrand in self.items():
      replace[domain_ischeme] = function.replace_arguments(integrand, arguments)
    return replace

  def contains( self, target ):
    return any( target in integrand.serialized[0] for integrand in self.values() )

  def __add__( self, other ):
    assert isinstance( other, Integral ) and self.shape == other.shape
    add = self.empty( self.shape )
    add.update( self )
    for domain_ischeme, integrand in other.items():
      try:
        add[domain_ischeme] += integrand
      except KeyError:
        add[domain_ischeme] = integrand
    return add

  def __neg__( self ):
    return self * -1

  def __sub__( self, other ):
    return self + (-other)

  def __mul__( self, other ):
    if not isinstance( other, numbers.Number ):
      return NotImplemented
    mul = self.empty( self.shape )
    mul.update({ domain_ischeme: integrand*other for domain_ischeme, integrand in self.items() })
    return mul

  def __rmul__( self, other ):
    if not isinstance( other, numbers.Number ):
      return NotImplemented
    return self * other

  def __truediv__( self, other ):
    if not isinstance( other, numbers.Number ):
      return NotImplemented
    return self * (1/other)


class ModelError( Exception ): pass


def solve_linear(target, residual, constrain=None, *, arguments=None):
  '''solve linear problem

  Parameters
  ----------
  target : :class:`str`
      Name of the target: a :class:`nutils.function.Argument` in ``residual``.
  residual : Integral
      Residual integral, depends on ``target``
  constrain : float vector
      Defines the fixed entries of the coefficient vector
  arguments : :class:`collections.abc.Mapping`
      Defines the values for :class:`nutils.function.Argument` objects in
      `residual`.  The ``target`` should not be present in ``arguments``.
      Optional.

  Returns
  -------
  vector
      Array of ``target`` values for which ``residual == 0``'''

  jacobian = residual.derivative( target )
  if jacobian.contains( _find_argument( target, residual ) ):
    raise ModelError( 'problem is not linear' )
  assert target not in (arguments or {}), '`target` should not be defined in `arguments`'
  arguments = collections.ChainMap(arguments or {}, {target: numpy.zeros(_find_argument(target, residual).shape)})
  res, jac = Integral.multieval(residual, jacobian, arguments=arguments)
  return jac.solve( -res, constrain=constrain )


def solve( gen_lhs_resnorm, tol=1e-10, maxiter=numpy.inf ):
  '''execute nonlinear solver

  Iterates over nonlinear solver until tolerance is reached. Example::

      lhs = solve( newton( target, residual ), tol=1e-5 )

  Parameters
  ----------
  gen_lhs_resnorm : generator
      Generates (lhs, resnorm) tuples
  tol : float
      Target residual norm
  maxiter : int
      Maximum number of iterations

  Returns
  -------
  vector
      Coefficient vector that corresponds to a smaller than ``tol`` residual.
  '''

  try:
    lhs, resnorm = next(gen_lhs_resnorm)
    resnorm0 = resnorm
    inewton = 0
    while resnorm > tol:
      if inewton >= maxiter:
        raise ModelError( 'tolerance not reached in {} iterations'.format(maxiter) )
      with log.context( 'iter {0} ({1:.0f}%)'.format( inewton, 100 * numpy.log(resnorm0/resnorm) / numpy.log(resnorm0/tol) ) ):
        log.info( 'residual: {:.2e}'.format(resnorm) )
        lhs, resnorm = next(gen_lhs_resnorm)
      inewton += 1
  except StopIteration:
    raise ModelError( 'generator stopped before reaching target tolerance' )
  else:
    log.info( 'tolerance reached in {} iterations with residual {:.2e}'.format(inewton, resnorm) )
    return lhs


def withsolve( f ):
  '''add a .solve method to (lhs,resnorm) iterators

  Introduces the convenient form::

      newton( target, residual ).solve( tol )

  Shorthand for::

      solve( newton( target, residual ), tol )
  '''

  @functools.wraps( f, updated=() )
  class wrapper:
    def __init__( self, *args, **kwargs ):
      self.iter = f( *args, **kwargs )
    def __next__( self ):
      return next( self.iter )
    def __iter__( self ):
      return self.iter
    def solve( self, *args, **kwargs ):
      return solve( self.iter, *args, **kwargs )
  return wrapper


@withsolve
def newton(target, residual, lhs0=None, constrain=None, nrelax=numpy.inf, minrelax=.1, maxrelax=.9, rebound=2**.5, *, arguments=None):
  '''iteratively solve nonlinear problem by gradient descent

  Generates targets such that residual approaches 0 using Newton procedure with
  line search based on a residual integral. Suitable to be used inside
  ``solve``.

  An optimal relaxation value is computed based on the following cubic
  assumption::

      | res( lhs + r * dlhs ) |^2 = A + B * r + C * r^2 + D * r^3

  where ``A``, ``B``, ``C`` and ``D`` are determined based on the current and
  updated residual and tangent.

  Parameters
  ----------
  target : :class:`str`
      Name of the target: a :class:`nutils.function.Argument` in ``residual``.
  residual : Integral
  lhs0 : vector
      Coefficient vector, starting point of the iterative procedure.
  constrain : boolean or float vector
      Equal length to ``lhs0``, masks the free vector entries as ``False``
      (boolean) or NaN (float). In the remaining positions the values of
      ``lhs0`` are returned unchanged (boolean) or overruled by the values in
      `constrain` (float).
  nrelax : int
      Maximum number of relaxation steps before proceding with the updated
      coefficient vector (by default unlimited).
  minrelax : float
      Lower bound for the relaxation value, to force re-evaluating the
      functional in situation where the parabolic assumption would otherwise
      result in unreasonably small steps.
  maxrelax : float
      Relaxation value below which relaxation continues, unless ``nrelax`` is
      reached; should be a value less than or equal to 1.
  rebound : float
      Factor by which the relaxation value grows after every update until it
      reaches unity.
  arguments : :class:`collections.abc.Mapping`
      Defines the values for :class:`nutils.function.Argument` objects in
      `residual`.  The ``target`` should not be present in ``arguments``.
      Optional.

  Yields
  ------
  vector
      Coefficient vector that approximates residual==0 with increasing accuracy
  '''

  assert target not in (arguments or {}), '`target` should not be defined in `arguments`'
  resolved_target = _find_argument( target, residual )

  if lhs0 is None:
    lhs0 = numpy.zeros(residual.shape)
  else:
    assert isinstance(lhs0, numpy.ndarray) and lhs0.dtype == float and lhs0.shape == residual.shape, 'invalid lhs0 argument'

  if constrain is None:
    constrain = numpy.zeros(residual.shape, dtype=bool)
  else:
    assert isinstance(constrain, numpy.ndarray) and constrain.dtype in (bool,float) and constrain.shape == residual.shape, 'invalid constrain argument'
    if constrain.dtype == float:
      lhs0 = numpy.choose(numpy.isnan(constrain), [constrain, lhs0])
      constrain = ~numpy.isnan(constrain)

  jacobian = residual.derivative( target )
  if not jacobian.contains( resolved_target ):
    log.info( 'problem is linear' )
    res, jac = Integral.multieval(residual, jacobian, arguments=collections.ChainMap(arguments or {}, {target: numpy.zeros(resolved_target.shape)}))
    cons = lhs0.copy()
    cons[~constrain] = numpy.nan
    lhs = jac.solve( -res, constrain=cons )
    yield lhs, 0
    return

  lhs = lhs0.copy()
  fcache = cache.WrapperCache()
  res, jac = Integral.multieval(residual, jacobian, fcache=fcache, arguments=collections.ChainMap(arguments or {}, {target: lhs}))
  zcons = numpy.zeros( len(resolved_target) )
  zcons[~constrain] = numpy.nan
  relax = 1
  while True:
    resnorm = numpy.linalg.norm( res[~constrain] )
    yield lhs, resnorm
    dlhs = -jac.solve( res, constrain=zcons )
    relax = min( relax * rebound, 1 )
    for irelax in itertools.count():
      res, jac = Integral.multieval(residual, jacobian, fcache=fcache, arguments=collections.ChainMap(arguments or {}, {target: lhs+relax*dlhs}))
      newresnorm = numpy.linalg.norm( res[~constrain] )
      if irelax >= nrelax:
        if newresnorm > resnorm:
          log.warning( 'failed to decrease residual' )
          return
        break
      if not numpy.isfinite( newresnorm ):
        log.info( 'failed to evaluate residual ({})'.format( newresnorm ) )
        newrelax = 0 # replaced by minrelax later
      else:
        r0 = resnorm**2
        d0 = -2 * r0
        r1 = newresnorm**2
        d1 = 2 * numpy.dot( jac.matvec(dlhs)[~constrain], res[~constrain] )
        log.info( 'line search: 0[{}]{} {}creased by {:.0f}%'.format( '---+++' if d1 > 0 else '--++--' if r1 > r0 else '------', round(relax,5), 'in' if newresnorm > resnorm else 'de', 100*abs(newresnorm/resnorm-1) ) )
        if r1 <= r0 and d1 <= 0:
          break
        D = 2*r0 - 2*r1 + d0 + d1
        if D > 0:
          C = 3*r1 - 3*r0 - 2*d0 - d1
          newrelax = ( numpy.sqrt(C**2-3*d0*D) - C ) / (3*D)
          log.info( 'minimum based on 3rd order estimation: {:.3f}'.format(newrelax) )
        else:
          C = r1 - r0 - d0
          # r1 > r0 => C > 0
          # d1 > 0  => C = r1 - r0 - d0/2 - d0/2 > r1 - r0 - d0/2 - d1/2 = -D/2 > 0
          newrelax = -.5 * d0 / C
          log.info( 'minimum based on 2nd order estimation: {:.3f}'.format(newrelax) )
        if newrelax > maxrelax:
          break
      relax *= max( newrelax, minrelax )
    lhs += relax * dlhs


@withsolve
def pseudotime(target, residual, inertia, timestep, lhs0, residual0=None, constrain=None, *, arguments=None):
  '''iteratively solve nonlinear problem by pseudo time stepping

  Generates targets such that residual approaches 0 using hybrid of Newton and
  time stepping. Requires an inertia term and initial timestep. Suitable to be
  used inside ``solve``.

  Parameters
  ----------
  target : :class:`str`
      Name of the target: a :class:`nutils.function.Argument` in ``residual``.
  residual : Integral
  inertia : Integral
  timestep : float
      Initial time step, will scale up as residual decreases
  lhs0 : vector
      Coefficient vector, starting point of the iterative procedure.
  constrain : boolean or float vector
      Equal length to ``lhs0``, masks the free vector entries as ``False``
      (boolean) or NaN (float). In the remaining positions the values of
      ``lhs0`` are returned unchanged (boolean) or overruled by the values in
      `constrain` (float).
  arguments : :class:`collections.abc.Mapping`
      Defines the values for :class:`nutils.function.Argument` objects in
      `residual`.  The ``target`` should not be present in ``arguments``.
      Optional.

  Yields
  ------
  vector, float
      Tuple of coefficient vector and residual norm
  '''

  assert target not in (arguments or {}), '`target` should not be defined in `arguments`'

  jacobian0 = residual.derivative( target )
  jacobiant = inertia.derivative( target )
  if residual0 is not None:
    residual += residual0

  if constrain is None:
    constrain = numpy.zeros(residual.shape, dtype=bool)
  else:
    assert isinstance(constrain, numpy.ndarray) and constrain.dtype in (bool,float) and constrain.shape == residual.shape, 'invalid constrain argument'
    if constrain.dtype == float:
      lhs0 = numpy.choose(numpy.isnan(constrain), [constrain, lhs0])
      constrain = ~numpy.isnan(constrain)

  zcons = util.NanVec( len(_find_argument(target, residual, residual0)) )
  zcons[constrain] = 0
  lhs = lhs0.copy()
  fcache = cache.WrapperCache()
  res, jac = Integral.multieval(residual, jacobian0+jacobiant/timestep, fcache=fcache, arguments=collections.ChainMap(arguments or {}, {target: lhs}))
  resnorm = resnorm0 = numpy.linalg.norm( res[~constrain] )
  while True:
    yield lhs, resnorm
    lhs -= jac.solve( res, constrain=zcons )
    thistimestep = timestep * (resnorm0/resnorm)
    log.info( 'timestep: {:.0e}'.format(thistimestep) )
    res, jac = Integral.multieval(residual, jacobian0+jacobiant/thistimestep, fcache=fcache, arguments=collections.ChainMap(arguments or {}, {target: lhs}))
    resnorm = numpy.linalg.norm( res[~constrain] )


def thetamethod(target, residual, inertia, timestep, lhs0, theta, target0=None, constrain=None, tol=1e-10, *, arguments=None, **newtonargs):
  '''solve time dependent problem using the theta method

  Parameters
  ----------
  target : :class:`str`
      Name of the target: a :class:`nutils.function.Argument` in ``residual``.
  residual : Integral
  inertia : Integral
  timestep : float
      Initial time step, will scale up as residual decreases
  lhs0 : vector
      Coefficient vector, starting point of the iterative procedure.
  theta : float
      Theta value (theta=1 for implicit Euler, theta=0.5 for Crank-Nicolson)
  residual0 : Integral
      Optional additional residual component evaluated in previous timestep
  constrain : boolean or float vector
      Equal length to ``lhs0``, masks the free vector entries as ``False``
      (boolean) or NaN (float). In the remaining positions the values of
      ``lhs0`` are returned unchanged (boolean) or overruled by the values in
      `constrain` (float).
  tol : float
      Residual tolerance of individual timesteps
  arguments : :class:`collections.abc.Mapping`
      Defines the values for :class:`nutils.function.Argument` objects in
      `residual`.  The ``target`` should not be present in ``arguments``.
      Optional.

  Yields
  ------
  vector
      Coefficient vector for all timesteps after the initial condition.
  '''

  assert target not in (arguments or {}), '`target` should not be defined in `arguments`'
  if target0:
    assert target0 not in (arguments or {}), '`target0` should not be defined in `arguments`'
  else:
    target0 = object()
  lhs = lhs0
  res0 = residual * theta + inertia / timestep
  res1 = residual * (1-theta) - inertia / timestep
  res = res0 + res1.replace({target: function.Argument(target0, lhs.shape)})
  while True:
    yield lhs
    lhs = newton(target, residual=res, lhs0=lhs, constrain=constrain, arguments=collections.ChainMap(arguments or {}, {target0: lhs}), **newtonargs).solve(tol=tol)


impliciteuler = functools.partial(thetamethod, theta=1)
cranknicolson = functools.partial(thetamethod, theta=0.5)


@log.title
def optimize(target, functional, droptol=None, lhs0=None, constrain=None, newtontol=None, *, arguments=None):
  '''find the minimizer of a given functional

  Parameters
  ----------
  target : :class:`str`
      Name of the target: a :class:`nutils.function.Argument` in ``residual``.
  functional : scalar Integral
      The functional the should be minimized by varying target
  droptol : :class:`float`
      Threshold for leaving entries in the return value at NaN if they do not
      contribute to the value of the functional.
  lhs0 : vector
      Coefficient vector, starting point of the iterative procedure (if
      applicable).
  constrain : boolean or float vector
      Equal length to ``lhs0``, masks the free vector entries as ``False``
      (boolean) or NaN (float). In the remaining positions the values of
      ``lhs0`` are returned unchanged (boolean) or overruled by the values in
      `constrain` (float).
  newtontol : float
      Residual tolerance of Newton procedure (if applicable)

  Yields
  ------
  vector
      Coefficient vector corresponding to the functional optimum
  '''

  assert target not in (arguments or {}), '`target` should not be defined in `arguments`'
  assert len(functional.shape) == 0, 'functional should be scalar'
  arg = _find_argument(target, functional)
  if lhs0 is None:
    lhs0 = numpy.zeros(arg.shape)
  else:
    assert isinstance(lhs0, numpy.ndarray) and lhs0.dtype == float and lhs0.shape == arg.shape, 'invalid lhs0 argument'
  if constrain is None:
    constrain = numpy.zeros(arg.shape, dtype=bool)
  else:
    assert isinstance(constrain, numpy.ndarray) and constrain.dtype in (bool,float) and constrain.shape == arg.shape, 'invalid constrain argument'
    if constrain.dtype == float:
      lhs0 = numpy.choose(numpy.isnan(constrain), [constrain, lhs0])
      constrain = ~numpy.isnan(constrain)
  residual = functional.derivative(target)
  jacobian = residual.derivative(target)
  f0, res, jac = Integral.multieval(functional, residual, jacobian, arguments=collections.ChainMap(arguments or {}, {target: lhs0}))
  nandofs = numpy.zeros(residual.shape, dtype=bool) if droptol is None else ~jac.rowsupp(droptol)
  cons = numpy.zeros(residual.shape)
  cons[~(constrain|nandofs)] = numpy.nan
  lhs = lhs0 - jac.solve(res, constrain=cons) # residual(lhs0) + jacobian(lhs0) dlhs = 0
  if not jacobian.contains(arg): # linear: functional(lhs0+dlhs) = functional(lhs0) + residual(lhs0) dlhs + .5 dlhs jacobian(lhs0) dlhs
    value = f0 + .5 * res.dot(lhs-lhs0)
  else: # nonlinear
    assert newtontol is not None, 'newton tolerance `newtontol` must be specified for nonlinear problems'
    lhs = newton(target, residual, lhs0=lhs, constrain=constrain|nandofs, arguments=arguments).solve(newtontol)
    value = functional.eval(arguments=collections.ChainMap(arguments or {}, {target: lhs}))
  assert not numpy.isnan(lhs[~(constrain|nandofs)]).any(), 'optimization failed (forgot droptol?)'
  log.info('optimum: {:.2e}'.format(value))
  lhs[nandofs] = numpy.nan
  return lhs