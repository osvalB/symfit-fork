"""
This module contains support functions and convenience methods used
throughout symfit. Some are used predominantly internally, others are
designed for users.
"""
from functools import wraps
from collections import OrderedDict
import inspect

import numpy as np
from sympy.utilities.lambdify import lambdify
import sympy

from symfit.core.argument import Parameter, Variable

def seperate_symbols(func):
    """
    Seperate the symbols in symbolic function func. Return them in alphabetical
    order.

    :param func: scipy symbolic function.
    :return: (vars, params), a tuple of all variables and parameters, each 
        sorted in alphabetical order.
    :raises TypeError: only symfit Variable and Parameter are allowed, not sympy
        Symbols.
    """
    params = []
    vars = []
    from sympy.tensor import IndexedBase
    from sympy import Symbol
    for symbol in func.free_symbols:
        if isinstance(symbol, Parameter):
            params.append(symbol)
        elif isinstance(symbol, (Variable, IndexedBase, Symbol)):
            vars.append(symbol)
        else:
            raise TypeError('model contains an unknown symbol type, {}'.format(type(symbol)))
    params.sort(key=lambda symbol: symbol.name)
    vars.sort(key=lambda symbol: symbol.name)
    return vars, params

def sympy_to_py(func, vars, params):
    """
    Turn a symbolic expression into a Python lambda function,
    which has the names of the variables and parameters as it's argument names.

    :param func: sympy expression
    :param vars: variables in this model
    :param params: parameters in this model
    :return: lambda function to be used for numerical evaluation of the model. Ordering of the arguments will be vars
        first, then params.
    """
    return lambdify((vars + params), func, modules='numpy', dummify=False)

def sympy_to_scipy(func, vars, params):
    """
    Convert a symbolic expression to one scipy digs. Not used by ``symfit`` any more.

    :param func: sympy expression
    :param vars: variables
    :param params: parameters
    :return: Scipy-style function to be used for numerical evaluation of the model.
    """
    lambda_func = sympy_to_py(func, vars, params)
    def f(x, p):
        """
        Scipy style function.

        :param x: list of arrays, NxM
        :param p: tuple of parameter values.
        """
        x = np.atleast_2d(x)
        y = [x[i] for i in range(len(x))] if len(x[0]) else []
        try:
            ans = lambda_func(*(y + list(p)))
        except TypeError:
            # Possibly this is a constant function in which case it only has Parameters.
            ans = lambda_func(*list(p))# * np.ones(x_shape)
        return ans

    return f

def variables(names):
    """
    Convenience function for the creation of multiple variables.

    :param names: string of variable names. Should be comma seperated.
        Example: x, y = variables('x, y')
    """
    return [Variable(name=name.strip()) for name in names.split(',')]

def parameters(names):
    """
    Convenience function for the creation of multiple parameters.

    :param names: string of parameter names. Should be comma seperated.
        Example: a, b = parameters('a, b')
    """
    return [Parameter(name=name.strip()) for name in names.split(',')]

def cache(func):
    """
    Decorator function that gets a method as its input and either buffers the input,
    or returns the buffered output. Used in conjunction with properties to take away
    the standard buffering logic.

    :param func:
    :return:
    """
    @wraps(func)
    def new_func(self):
        try:
            return getattr(self, '_{}'.format(func.__name__))
        except AttributeError:
            setattr(self, '_{}'.format(func.__name__), func(self))
            return getattr(self, '_{}'.format(func.__name__))

    return new_func

def jacobian(expr, symbols):
    """
    Derive a symbolic expr w.r.t. each symbol in symbols. This returns a symbolic jacobian vector.

    :param expr: A sympy Expr.
    :param symbols: The symbols w.r.t. which to derive.
    """
    jac = []
    for symbol in symbols:
        # Differentiate to every param
        f = sympy.diff(expr, symbol)
        jac.append(f)
    return jac

class RequiredKeyword(object):
    """ Flag variable to indicate that this is a required keyword. """

class RequiredKeywordError(Exception):
    """ Error raised in case a keyword-only argument is not treated as such. """

class keywordonly(object):
    """
    Decorator class which wraps a python 2 function into one with keyword-only arguments.
    Is it as beautiful as the official syntax? obviously, no. But it's the best looking alternative.
    """
    def __init__(self, **kwonly_arguments):
        self.kwonly_arguments = kwonly_arguments
        self.required_keywords = {kw: value for kw, value in kwonly_arguments.items() if value is RequiredKeyword}
        self.optional_keywords = {kw: value for kw, value in kwonly_arguments.items() if value is not RequiredKeyword}

    def __call__(self, func):
        argspec = inspect.getargspec(func)
        try:  # All the kwonly_arguments should be presents as normal args to the func.
            assert set(argspec.args) == set(self.kwonly_arguments)
        except AssertionError:
            raise RequiredKeywordError('All keyword-only arguments should be present as positional arguments in the function definition.')

        @wraps(func)
        def wrapped_func(*args, **kwargs):
            """
            :param args: args used to call the function
            :param kwargs: kwargs used to call the function
            :return: Wrapped function which behaves like it has keyword-only arguments.
            :raises: ``RequiredKeywordError`` if not all required keywords where specified.
            """
            for kw in self.required_keywords:
                if kw not in kwargs:
                    raise RequiredKeywordError('Keyword `{}` is a required keyword. Please provide a value.'.format(kw))
            else:  # All required keywords were provided. We can now safely call the function!
                for kw, value in self.optional_keywords.items():
                    if kw not in kwargs:
                        kwargs[kw] = value
                return func(*args, **kwargs)

        return wrapped_func