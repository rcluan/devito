"""The Iteration/Expression Tree (IET) hierarchy."""

import abc
import inspect
import numbers
from cached_property import cached_property
from collections import Iterable, OrderedDict, namedtuple

import cgen as c

from devito.cgen_utils import ccode
from devito.data import FULL
from devito.ir.equations import ClusterizedEq
from devito.ir.iet import (IterationProperty, SEQUENTIAL, PARALLEL, PARALLEL_IF_ATOMIC,
                           VECTOR, ELEMENTAL, REMAINDER, WRAPPABLE, AFFINE, tagger, ntags,
                           REDUNDANT)
from devito.ir.support import Forward, detect_io
from devito.parameters import configuration
from devito.symbolics import FunctionFromPointer, as_symbol
from devito.tools import (Signer, as_tuple, filter_ordered, filter_sorted, flatten,
                          validate_type, dtype_to_cstr)
from devito.types import Dimension, Symbol, Indexed
from devito.types.basic import AbstractFunction

__all__ = ['Node', 'Block', 'Denormals', 'Expression', 'Element', 'Callable',
           'Call', 'Conditional', 'Iteration', 'List', 'LocalExpression', 'Section',
           'TimedList', 'MetaCall', 'ArrayCast', 'ForeignExpression', 'HaloSpot',
           'IterationTree', 'ExpressionBundle', 'Increment']

# First-class IET nodes


class Node(Signer):

    __metaclass__ = abc.ABCMeta

    is_Node = True
    is_Block = False
    is_Iteration = False
    is_IterationFold = False
    is_Expression = False
    is_Increment = False
    is_ForeignExpression = False
    is_Callable = False
    is_Call = False
    is_List = False
    is_Element = False
    is_Section = False
    is_HaloSpot = False
    is_ExpressionBundle = False

    _traversable = []
    """
    :attr:`_traversable`. The traversable fields of the Node; that is, fields
    walked over by a :class:`Visitor`. All arguments in __init__ whose name
    appears in this list are treated as traversable fields.
    """

    def __new__(cls, *args, **kwargs):
        obj = super(Node, cls).__new__(cls)
        argnames = inspect.getfullargspec(cls.__init__).args
        obj._args = {k: v for k, v in zip(argnames[1:], args)}
        obj._args.update(kwargs.items())
        obj._args.update({k: None for k in argnames[1:] if k not in obj._args})
        return obj

    def _rebuild(self, *args, **kwargs):
        """Reconstruct self. None of the embedded Sympy expressions are rebuilt."""
        handle = self._args.copy()  # Original constructor arguments
        argnames = [i for i in self._traversable if i not in kwargs]
        handle.update(OrderedDict([(k, v) for k, v in zip(argnames, args)]))
        handle.update(kwargs)
        return type(self)(**handle)

    @property
    def ccode(self):
        """
        Generate C code.

        This is a shorthand for

            .. code-block:: python

              from devito.ir.iet import CGen
              CGen().visit(self)
        """
        from devito.ir.iet.visitors import CGen
        return CGen().visit(self)

    @property
    def view(self):
        """A representation of the IET rooted in ``self``."""
        from devito.ir.iet.visitors import printAST
        return printAST(self)

    @property
    def children(self):
        """Return the traversable children."""
        return tuple(getattr(self, i) for i in self._traversable)

    @property
    def args(self):
        """Arguments used to construct the Node."""
        return self._args.copy()

    @property
    def args_frozen(self):
        """Arguments used to construct the Node that cannot be traversed."""
        return {k: v for k, v in self.args.items() if k not in self._traversable}

    def __str__(self):
        return str(self.ccode)

    @abc.abstractproperty
    def functions(self):
        """All :class:`AbstractFunction` objects used by this node."""
        raise NotImplementedError()

    @abc.abstractproperty
    def free_symbols(self):
        """All :class:`Symbol` objects used by this node."""
        raise NotImplementedError()

    @abc.abstractproperty
    def defines(self):
        """All :class:`Symbol` objects defined by this node."""
        raise NotImplementedError()

    def _signature_items(self):
        return (str(self.ccode),)


class Block(Node):

    """A sequence of nodes, wrapped in a block {...}."""

    is_Block = True

    _traversable = ['body']

    def __init__(self, header=None, body=None, footer=None):
        self.header = as_tuple(header)
        self.body = as_tuple(body)
        self.footer = as_tuple(footer)

    def __repr__(self):
        return "<%s (%d, %d, %d)>" % (self.__class__.__name__, len(self.header),
                                      len(self.body), len(self.footer))

    @property
    def functions(self):
        return ()

    @property
    def free_symbols(self):
        return ()

    @property
    def defines(self):
        return ()


class List(Block):

    """A sequence of nodes."""

    is_List = True


class Element(Node):

    """
    A generic node. Can be a comment, a statement, ... or anything that cannot
    be expressed through an IET type.
    """

    is_Element = True

    def __init__(self, element):
        assert isinstance(element, (c.Comment, c.Statement, c.Value, c.Initializer,
                                    c.Pragma, c.Line, c.Assign, c.POD))
        self.element = element

    def __repr__(self):
        return "Element::\n\t%s" % (self.element)


class Call(Node):

    """A function call."""

    is_Call = True

    def __init__(self, name, params=None):
        self.name = name
        self.params = as_tuple(params)

    def __repr__(self):
        return "Call::\n\t%s(...)" % self.name

    @property
    def functions(self):
        return tuple(p for p in self.params if isinstance(p, AbstractFunction))

    @cached_property
    def free_symbols(self):
        free = set()
        for p in self.params:
            if isinstance(p, numbers.Number):
                continue
            free.update(p.free_symbols)
        # HACK: Filter dimensions to avoid them on popping onto outer parameters
        free = tuple(s for s in free if not isinstance(s, Dimension))
        return free

    @property
    def defines(self):
        return ()


class Expression(Node):

    """A node encapsulating a :class:`ClusterizedEq`."""

    is_Expression = True

    @validate_type(('expr', ClusterizedEq))
    def __init__(self, expr):
        self.expr = expr
        self.__expr_finalize__()

    def __expr_finalize__(self):
        """Finalize the Expression initialization."""
        self._functions = tuple(filter_ordered(flatten(detect_io(self.expr, relax=True))))
        self._dimensions = flatten(i.indices for i in self.functions if i.is_Indexed)
        self._dimensions = tuple(filter_ordered(self._dimensions))

    def __repr__(self):
        return "<%s::%s>" % (self.__class__.__name__,
                             filter_ordered([f.func for f in self.functions]))

    @property
    def dtype(self):
        return self.expr.dtype

    @property
    def output(self):
        """The symbol this Expression writes to."""
        return self.expr.lhs

    @property
    def write(self):
        """The :class:`Function` this Expression writes to."""
        return self.expr.lhs.function

    @property
    def dimensions(self):
        return self._dimensions

    @property
    def is_scalar(self):
        """True if a scalar expression, False otherwise."""
        return self.expr.lhs.is_Symbol

    @property
    def is_tensor(self):
        """True if a tensor expression, False otherwise."""
        return not self.is_scalar

    @property
    def is_scalar_assign(self):
        """True if a scalar, non-increment expression."""
        return self.is_scalar and not self.is_Increment

    @property
    def defines(self):
        return (self.write,) if self.is_scalar else ()

    @property
    def free_symbols(self):
        return tuple(self.expr.free_symbols)

    @property
    def functions(self):
        return self._functions


class Increment(Expression):

    """A node representing a += increment."""

    is_Increment = True


class Iteration(Node):

    """
    Implement a for-loop over nodes.

    Parameters
    ----------
    nodes : Node or list of Node
        The for-loop body.
    dimension : Dimension
        The Dimension over which the for-loop iterates.
    limits : expr-like or 3-tuple
        If an expression, it represents the for-loop max point; in this case, the
        min point is 0 and the step increment is unitary. If a 3-tuple, the
        format is ``(min point, max point, stepping)``.
    offsets : 2-tuple of ints, optional
        Additional offsets ``(min_ofs, max_ofs)`` to be honoured by the for-loop.
        Defaults to (0, 0).
    direction: IterationDirection, optional
        The for-loop direction. Accepted:
        - ``Forward``: i += stepping (defaults)
        - ``Backward``: i -= stepping
    properties : IterationProperty or list of IterationProperty, optional
        Iteration decorators, denoting properties such as parallelism.
    pragmas : cgen.Pragma or list of cgen.Pragma, optional
        A bag of pragmas attached to this Iteration.
    uindices : DerivedDimension or list of DerivedDimension, optional
        An uindex is an additional iteration variable defined by the for-loop. The
        for-loop bounds are independent of all ``uindices`` (hence the name uindex,
        or "unbounded index"). An uindex must have ``dimension`` as its parent.
    """

    is_Iteration = True

    _traversable = ['nodes']

    def __init__(self, nodes, dimension, limits, offsets=None, direction=None,
                 properties=None, pragmas=None, uindices=None):
        self.nodes = as_tuple(nodes)
        self.dim = dimension
        self.index = self.dim.name
        self.direction = direction or Forward

        # Generate loop limits
        if isinstance(limits, Iterable):
            assert(len(limits) == 3)
            self.limits = tuple(limits)
        elif self.dim.is_Incr:
            self.limits = (self.dim.symbolic_min, limits, self.dim.step)
        else:
            self.limits = (0, limits, 1)

        # Record offsets to later adjust loop limits accordingly
        self.offsets = (0, 0) if offsets is None else as_tuple(offsets)
        assert len(self.offsets) == 2

        # Track this Iteration's properties, pragmas and unbounded indices
        properties = as_tuple(properties)
        assert (i in IterationProperty._KNOWN for i in properties)
        self.properties = as_tuple(filter_sorted(properties))
        self.pragmas = as_tuple(pragmas)
        self.uindices = as_tuple(uindices)
        assert all(i.is_Derived and i.root is self.dim for i in self.uindices)

    def __repr__(self):
        properties = ""
        if self.properties:
            properties = [str(i) for i in self.properties]
            properties = "WithProperties[%s]::" % ",".join(properties)
        index = self.index
        if self.uindices:
            index += '[%s]' % ','.join(i.name for i in self.uindices)
        return "<%sIteration %s; %s>" % (properties, index, self.limits)

    @property
    def is_Affine(self):
        return AFFINE in self.properties

    @property
    def is_Sequential(self):
        return SEQUENTIAL in self.properties

    @property
    def is_Parallel(self):
        return PARALLEL in self.properties

    @property
    def is_ParallelAtomic(self):
        return PARALLEL_IF_ATOMIC in self.properties

    @property
    def is_ParallelRelaxed(self):
        return self.is_Parallel or self.is_ParallelAtomic

    @property
    def is_Vectorizable(self):
        return VECTOR in self.properties

    @property
    def is_Elementizable(self):
        return ELEMENTAL in self.properties

    @property
    def is_Wrappable(self):
        return WRAPPABLE in self.properties

    @property
    def is_Remainder(self):
        return REMAINDER in self.properties

    @property
    def ncollapsed(self):
        for i in self.properties:
            if i.name == 'collapsed':
                return i.val
        return 0

    @property
    def tag(self):
        for i in self.properties:
            if i.name == 'tag':
                return i.val
        return None

    def retag(self, tag_value=None):
        """
        Create a new Iteration object which is identical to ``self``, except
        for the tag. If provided, ``tag_value`` is used as new tag; otherwise,
        an internally generated tag is used.
        """
        if self.tag is None:
            return self._rebuild()
        properties = [tagger(tag_value or (ntags() + 1)) if i.name == 'tag' else i
                      for i in self.properties]
        return self._rebuild(properties=properties)

    @property
    def symbolic_bounds(self):
        """A 2-tuple representing the symbolic bounds [min, max] of the Iteration."""
        _min = self.limits[0]
        _max = self.limits[1]
        try:
            _min = as_symbol(_min)
        except TypeError:
            # A symbolic expression
            pass
        try:
            _max = as_symbol(_max)
        except TypeError:
            # A symbolic expression
            pass
        return (_min + as_symbol(self.offsets[0]), _max + as_symbol(self.offsets[1]))

    @property
    def symbolic_size(self):
        """The symbolic size of the Iteration."""
        return self.symbolic_bounds[1] - self.symbolic_bounds[0] + 1

    @property
    def symbolic_min(self):
        """The symbolic min of the Iteration."""
        return self.symbolic_bounds[0]

    @property
    def symbolic_max(self):
        """The symbolic max of the Iteration."""
        return self.symbolic_bounds[1]

    @property
    def symbolic_incr(self):
        """The symbolic increment of the Iteration."""
        return self.limits[2]

    def bounds(self, _min=None, _max=None):
        """
        The bounds [min, max] of the Iteration, as numbers if min/max are supplied,
        as symbols otherwise.
        """
        _min = _min if _min is not None else self.limits[0]
        _max = _max if _max is not None else self.limits[1]

        _min = _min + self.offsets[0]
        _max = _max + self.offsets[1]

        return (_min, _max)

    def size(self, _min=None, _max=None):
        """The size of the iteration space if _min/_max are supplied, None otherwise."""
        _min, _max = self.bounds(_min, _max)
        return _max - _min + 1

    @property
    def functions(self):
        """All :class:`Function`s appearing in the Iteration header."""
        return ()

    @property
    def free_symbols(self):
        """All :class:`Symbol`s appearing in the Iteration header."""
        return tuple(self.symbolic_min.free_symbols) \
            + tuple(self.symbolic_max.free_symbols) \
            + self.uindices \
            + tuple(flatten(i.symbolic_min.free_symbols for i in self.uindices)) \
            + tuple(flatten(i.symbolic_incr.free_symbols for i in self.uindices))

    @property
    def defines(self):
        """All :class:`Symbol`s defined in the Iteration header."""
        return self.dimensions

    @property
    def dimensions(self):
        """All :class:`Dimension`s appearing in the Iteration header."""
        return tuple(self.dim._defines) + self.uindices

    @property
    def write(self):
        """All :class:`Function`s written to in this :class:`Iteration`"""
        return []


class Callable(Node):

    """
    A callable function.

    Parameters
    ----------
    name : str
        The name of the callable.
    body : Node or list of Node
        The Callable body.
    retval : str
        The return type of Callable.
    parameters : list of :class:`Basic`, optional
        The objects in input to the Callable.
    prefix : list of str, optional
        Qualifiers to prepend to the Callable signature. Defaults to ``('static',
        'inline')``.
    """

    is_Callable = True

    _traversable = ['body']

    def __init__(self, name, body, retval, parameters=None, prefix=('static', 'inline')):
        self.name = name
        self.body = as_tuple(body)
        self.retval = retval
        self.prefix = as_tuple(prefix)
        self.parameters = as_tuple(parameters)

    def __repr__(self):
        parameters = ",".join(['void*' if i.is_Object else dtype_to_cstr(i.dtype)
                               for i in self.parameters])
        return "%s[%s]<%s; %s>" % (self.__class__.__name__, self.name, self.retval,
                                   parameters)


class Conditional(Node):

    """
    A node to express if-then-else blocks.

    Parameters
    ----------
    condition : expr-like
        The if condition.
    then_body : Node or list of Node
        The then body.
    else_body : Node or list of Node
        The else body.
    """

    is_Conditional = True

    _traversable = ['then_body', 'else_body']

    def __init__(self, condition, then_body, else_body=None):
        self.condition = condition
        self.then_body = as_tuple(then_body)
        self.else_body = as_tuple(else_body)

    def __repr__(self):
        if self.else_body:
            return "<[%s] ? [%s] : [%s]>" %\
                (ccode(self.condition), repr(self.then_body), repr(self.else_body))
        else:
            return "<[%s] ? [%s]" % (ccode(self.condition), repr(self.then_body))

    @property
    def functions(self):
        ret = []
        for i in self.condition.free_symbols:
            try:
                ret.append(i.function)
            except AttributeError:
                pass
        return tuple(ret)

    @property
    def free_symbols(self):
        return tuple(self.condition.free_symbols)

    @property
    def defines(self):
        return ()


# Second level IET nodes

class TimedList(List):

    """
    Wrap a Node with C-level timers.

    Parameters
    ----------
    timer : Timer
        The Timer used by the TimedList.
    lname : str
        A unique name for the timed code block.
    body : Node or list of Node
        The TimedList body.
    """

    def __init__(self, timer, lname, body):
        self._name = lname
        self._timer = timer
        header = [c.Statement("struct timeval start_%s, end_%s" % (lname, lname)),
                  c.Statement("gettimeofday(&start_%s, NULL)" % lname)]
        footer = [c.Statement("gettimeofday(&end_%s, NULL)" % lname),
                  c.Statement(("%(gn)s->%(ln)s += " +
                               "(double)(end_%(ln)s.tv_sec-start_%(ln)s.tv_sec)+" +
                               "(double)(end_%(ln)s.tv_usec-start_%(ln)s.tv_usec)" +
                               "/1000000") % {'gn': timer.name, 'ln': lname})]
        super(TimedList, self).__init__(header, body, footer)

    @property
    def name(self):
        return self._name

    @property
    def timer(self):
        return self._timer

    @property
    def free_symbols(self):
        return (self.timer,)


class Denormals(List):

    """Macros to make sure denormal numbers are flushed in hardware."""

    def __init__(self, header=None, body=None, footer=None):
        b = [Element(c.Comment('Flush denormal numbers to zero in hardware')),
             Element(c.Statement('_MM_SET_DENORMALS_ZERO_MODE(_MM_DENORMALS_ZERO_ON)')),
             Element(c.Statement('_MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON)'))]
        super(Denormals, self).__init__(header, b, footer)

    def __repr__(self):
        return "<DenormalsMacro>"


class ArrayCast(Node):

    """
    A node encapsulating a cast of a raw C pointer to a multi-dimensional array.
    """

    def __init__(self, function):
        self.function = function

    @property
    def castshape(self):
        """The shape used in the left-hand side and right-hand side of the ArrayCast."""
        if configuration['codegen'] == 'explicit' or self.function.is_Array:
            return self.function.symbolic_shape[1:]
        else:
            return tuple(self.function._C_get_field(FULL, d).size
                         for d in self.function.dimensions[1:])

    @property
    def functions(self):
        return (self.function,)

    @property
    def free_symbols(self):
        """
        The symbols required by the ArrayCast.

        This may include DiscreteFunctions as well as Dimensions.
        """
        if configuration['codegen'] == 'explicit' or self.function.is_Array:
            sizes = flatten(s.free_symbols for s in self.function.symbolic_shape[1:])
            return (self.function, ) + as_tuple(sizes)
        else:
            return (self.function,)

    @property
    def defines(self):
        return ()


class LocalExpression(Expression):

    """
    A node encapsulating a SymPy equation which also defines its LHS.
    """

    @property
    def defines(self):
        return (self.write, )


class ForeignExpression(Expression):

    """A node representing a SymPy :class:`FunctionFromPointer` expression."""

    is_ForeignExpression = True

    @validate_type(('expr', FunctionFromPointer),
                   ('dtype', type))
    def __init__(self, expr, dtype, **kwargs):
        self.expr = expr
        self._dtype = dtype
        self._is_increment = kwargs.get('is_Increment', False)
        self.__expr_finalize__()

    @property
    def dtype(self):
        return self._dtype

    @property
    def output(self):
        return self.expr.base

    @property
    def write(self):
        if isinstance(self.output, (Symbol, Indexed)):
            return self.output.function
        else:
            return None

    @property
    def is_Increment(self):
        return self._is_increment

    @property
    def is_scalar(self):
        return False

    @property
    def is_tensor(self):
        return False


class Section(List):

    """
    A sequence of nodes.

    Functionally, a :class:`Section` is identical to a :class:`List`; that is,
    they generate the same code (i.e., their ``body``). However, a Section should
    be used to define sub-trees that, for some reasons, have a relevance within
    the IET (e.g., groups of statements that logically represent the same
    computation unit).
    """

    is_Sequence = True

    def __init__(self, name, body=None):
        super(Section, self).__init__(body=body)
        self.name = name

    def __repr__(self):
        return "<Section (%d)>" % len(self.body)

    @property
    def roots(self):
        return self.body


class HaloSpot(List):

    """
    A node representing an MPI halo exchange.
    """

    is_HaloSpot = True

    def __init__(self, halo_scheme, body=None, properties=None):
        super(HaloSpot, self).__init__(body=body)
        self.halo_scheme = halo_scheme
        self.properties = as_tuple(properties)

    @property
    def fmapper(self):
        return self.halo_scheme.fmapper

    @property
    def mask(self):
        return self.halo_scheme.mask

    @property
    def is_Redundant(self):
        return REDUNDANT in self.properties

    def __repr__(self):
        redundant = "[redundant]" if self.is_Redundant else ""
        return "<HaloSpot%s>" % redundant


class ExpressionBundle(List):

    """
    A sequence of :class:`Expression`s.
    """

    is_ExpressionBundle = True

    def __init__(self, shape, ops, traffic, body=None):
        super(ExpressionBundle, self).__init__(body=body)
        self.shape = shape
        self.ops = ops
        self.traffic = traffic

    def __repr__(self):
        return "<ExpressionBundle (%d)>" % len(self.exprs)

    @property
    def exprs(self):
        return self.body


# Utility classes


class IterationTree(tuple):

    """
    Represent a sequence of nested :class:`Iteration`s.
    """

    @property
    def root(self):
        return self[0] if self else None

    @property
    def inner(self):
        return self[-1] if self else None

    @property
    def dimensions(self):
        return [i.dim for i in self]

    def __repr__(self):
        return "IterationTree%s" % super(IterationTree, self).__repr__()

    def __getitem__(self, key):
        ret = super(IterationTree, self).__getitem__(key)
        return IterationTree(ret) if isinstance(key, slice) else ret


MetaCall = namedtuple('MetaCall', 'root local')
"""
Metadata for :class:`Callable`s. ``root`` is a pointer to the callable
Iteration/Expression tree. ``local`` is a boolean indicating whether the
definition of the callable is known or not.
"""
