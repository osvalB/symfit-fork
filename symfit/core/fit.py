import abc
from collections import namedtuple, defaultdict, Mapping
import itertools
import functools
import inspect
import copy

import sympy
import numpy as np
from scipy.optimize import minimize

from symfit.core.argument import Parameter, Variable
from symfit.core.support import seperate_symbols, sympy_to_scipy, sympy_to_py, cache, r_squared, jacobian
from symfit.core.leastsqbound import leastsqbound


class ParameterDict(object):
    """
    Behaves like a dict when **-ed, allowing the sexy syntax where a model is
    called with values for the Variables and **params. However, under iteration
    it behaves like a list! In other words, it preserves order in the params.
    """
    def __init__(self, params, popt, pcov, *args, **kwargs):
        super(ParameterDict, self).__init__(*args, **kwargs)
        self.__params = params  # list of Parameter instances
        self.__params_dict = dict([(p.name, p) for p in params])
        # popt and pstdev are dicts with parameter names: value pairs.
        self.__popt = dict([(p.name, value) for p, value in zip(params, popt)])
        if pcov is not None:
            # Can be None.
            stdevs = np.sqrt(np.diagonal(pcov))
        else:
            stdevs = [None for _ in params]
        self.__pstdev = dict([(p.name, s) for p, s in zip(params, stdevs)])
        # Covariance matrix
        self.__pcov = pcov

    def __len__(self):
        return len(self.__params)

    def __iter__(self):
        return iter(self.__params)

    def __getitem__( self, key):
        """
        Intended use for this is for use of ParameterDict as a kwarg.
        Therefore return the value of the param, as this is what the user
        expects.
        :return: getattr(self, key), the value of the param with name 'key'
        """
        return getattr(self, key)

    def keys(self):
        return self.__params_dict.keys()

    def __getattr__(self, name):
        """
        A user can access the value of a parameter directly through this object.
        :param name: Name of a param in __params.
        Naming convention:
        let a = Parameter(). Then:
        .a gives the value of the parameter.
        .a_stdev gives the standard deviation.
        """
        # If a parameter with this name exists, return it immediately
        try:
            return self.__popt[name]
        except KeyError:
            param_name = name
            # Expand this if statement if in the future we allow more suffixes
            if name.endswith('_stdev'):
                param_name = name[:-len('_stdev')]  # everything but the suffix
                try:
                    return self.__pstdev[param_name]
                except KeyError:
                    pass
        raise AttributeError('No Parameter by the name {}.'.format(param_name))

    def get_value(self, param):
        """
        :param param: Parameter object.
        :return: returns the numerical value of param
        """
        assert isinstance(param, Parameter)
        return self.__popt[param.name]

    def get_stdev(self, param):
        """
        :param param: Parameter object.
        :return: returns the standard deviation of param
        """
        assert isinstance(param, Parameter)
        return self.__pstdev[param.name]


class FitResults(object):
    """
    Class to display the results of a fit in a nice and unambiguous way.
    All things related to the fit are available on this class, e.g.
    - paramameters + stdev
    - R squared (Regression coefficient.)
    - fitting status message

    This object is made to behave entirely read-only. This is a bit unnatural
    to enforce in Python but I feel it is necessary to guarantee the integrity
    of the results.
    """
    __params = None  # Private property.
    __infodict = None
    __status_message = None
    __iterations = None
    __ydata = None
    __sigma = None

    def __init__(self, params, popt, pcov, infodic, mesg, ier, ydata, sigma=None):
        """
        Excuse the ugly names of most of these variables, they are inherited
        from scipy.
        :param params:
        :param popt:
        :param pcov:
        :param infodic:
        :param mesg:
        :param ier:
        :param ydata:
        :return:
        """
        # Validate the types in rough way
        assert(type(infodic) == dict)
        self.__infodict = infodic
        assert(type(mesg) == str)
        self.__status_message = mesg
        assert(type(ier) == int)
        self.__iterations = ier
        # assert(type(ydata) == np.ndarray)
        self.__ydata = ydata
        self.__params = ParameterDict(params, popt, pcov)
        self.__sigma = sigma

    def __str__(self):
        res = '\nParameter Value        Standard Deviation\n'
        for p in self.params:
            value = self.params.get_value(p)
            value_str = '{:e}'.format(value) if value is not None else 'None'
            stdev = self.params.get_stdev(p)
            stdev_str = '{:e}'.format(stdev) if stdev is not None else 'None'
            res += '{:10}{} {}\n'.format(p.name, value_str, stdev_str, width=20)

        res += 'Fitting status message: {}\n'.format(self.status_message)
        res += 'Number of iterations:   {}\n'.format(self.infodict['nfev'])
        res += 'Regression Coefficient: {}\n'.format(self.r_squared)
        return res

    #
    # READ-ONLY Properties
    # What follows are all the read-only properties of this object.
    # Their definitions are mostly trivial, but necessary to make sure that
    # FitResults can't be changed.
    #

    @property
    def infodict(self):
        return self.__infodict

    @property
    def status_message(self):
        return self.__status_message

    @property
    def iterations(self):
        return self.__iterations

    @property
    def params(self):
        return self.__params

    @property
    def r_squared(self):
        """
        Getter for the r_squared property.
        :return: Regression coefficient.
        """
        if self.__ydata is not None:
            ss_err = np.sum(self.infodict['fvec'] ** 2)
            if self.__sigma is not None:
                ss_tot = np.sum(((self.__ydata - self.__ydata.mean())/self.__sigma) ** 2)
            else:
                ss_tot = np.sum((self.__ydata - self.__ydata.mean()) ** 2)
            # ss_tot = np.sum((self.__ydata - self.__ydata.mean()) ** 2)
            return 1 - (ss_err / ss_tot)
        else:
            return float('nan')

class Model(object):
    """
    Model represents a symbolic function and all it's derived properties such as sum of squares, jacobian etc.
    Models can be initiated from several objects:
    a = Model.from_dict({y: x**2})
    b = Model(y=x**2)

    Models are callable. The following rules apply to the ordering of the arguments:
    - first independent variables, then dependent variables, then parameters.
    - within each of these groups they are ordered alphabetically.
    """
    def __init__(self, *args, **kwargs):
        model_dict = {sympy.Dummy('y_{}'.format(index + 1)): expr for index, expr in enumerate(args)}
        model_dict.update(
            {Variable(name=dep_var_name): expr for dep_var_name, expr in kwargs.items()}
        )
        self._init_from_dict(model_dict)

    @classmethod
    def from_dict(cls, model_dict):
        self = cls()
        self._init_from_dict(model_dict)

        return self

    def _init_from_dict(self, model_dict):
        """
        Initiate self from a model_dict to make sure attributes such as vars, params are available.
        :param model_dict: dict of (dependent_var, expression) pairs.
        :return:
        """
        self.model_dict = model_dict
        self.dependent_vars = sorted(model_dict.keys(), key=lambda symbol: symbol.name)

        # Extract all the params and vars as a sorted, unique list.
        expressions = model_dict.values()
        self.params, self.independent_vars = set([]), set([])
        for expression in expressions:
            vars, params = seperate_symbols(expression)
            self.params.update(params)
            self.independent_vars.update(vars)
        # Although unique now, params and vars should be sorted alphabetically to prevent ambiguity
        self.params = sorted(self.params, key=lambda symbol: symbol.name)
        self.independent_vars = sorted(self.independent_vars, key=lambda symbol: symbol.name)
        # Make Variable object corresponding to each var.
        self.sigmas = {var: Variable(name='sigma_{}'.format(var.name)) for var in self.dependent_vars}

    @property
    @cache
    def chi_squared(self):
        return sum((y - f)**2/self.sigmas[y]**2 for y, f in self.model_dict.items())

    @property
    @cache
    def chi(self):
        """
        :return: Square root of the sum of squares. Required for MINPACK optimization only.
        """
        return sympy.sqrt(self.chi_squared)

    @property
    @cache
    def jacobian(self):
        """
        Return a symbolic jacobian of the chi function.
        Vector of derivatives w.r.t. each parameter.
        """
        jac = []
        for param in self.params:
            # Differentiate to every param
            f = sympy.diff(self.chi, param)
            jac.append(f)
        return jac

    # @staticmethod
    # def jacobian(expr, symbols):
    #     """
    #     Derive expr w.r.t. each symbol in symbols. This returns a jacobian vector.
    #     """
    #     jac = []
    #     for symbol in symbols:
    #         # Differentiate to every param
    #         f = sympy.diff(expr, symbol)
    #         jac.append(f)
    #     return jac

    @property
    @cache
    def ss_res(self):
        """
        :return: Residual sum of squares. Similar to chi_squared, but without considering weights.
        """
        return sum((y - f)**2 for y, f in self.model_dict.items())

    @property
    @cache
    def numerical_chi_squared(self):
        """
        :return: lambda function of the chi squared expression, to be used in numerical optimisation.
        """
        return sympy_to_py(self.chi_squared, self.vars, self.params)

    @property
    @cache
    def numerical_chi(self):
        """
        :return: lambda function of the chi expression, to be used in MINPACK optimisation.
        """
        return sympy_to_py(self.chi, self.vars, self.params)

    @property
    @cache
    def numerical_jacobian(self):
        """
        :return: lambda function of the jacobian, which can be used in numerical optimization.
        """
        return [sympy_to_py(component, self.vars, self.params) for component in self.jacobian]

    # @property
    # @cache
    # def numerical_jacobian(self):
    #     """
    #     :return: lambda function of the jacobian, which can be used in numerical optimization.
    #     """
    #     return [sympy_to_py(component, self.vars, self.params) for component in self.jacobian(self.chi, self.params)]

    @property
    @cache
    def vars(self):
        """ Returns both dependent, independent and sigma variables. """
        return self.independent_vars + self.dependent_vars + [self.sigmas[var] for var in self.dependent_vars]

    @property
    def bounds(self):
        """
        :return: List of tuples of all bounds on parameters.
        """
        return [(np.nextafter(p.value, 0), p.value) if p.fixed else (p.min, p.max) for p in self.params]


class BaseFit(metaclass=abc.ABCMeta):
    # __metaclass__  = abc.ABCMeta

    def __init__(self, model, *args, absolute_sigma=None, **kwargs):
        """
        :param model: sympy expression.
        :param absolute_sigma bool: True by default. If the sigma is only used
        for relative weights in your problem, you could consider setting it to
        False, but if your sigma are measurement errors, keep it at True.
        Note that curve_fit has this set to False by default, which is wrong in
        experimental science.
        :param args: data for dependent and independent variables. Assigned in
        alphabetical order, and independent vars are assigned first, then dependent
        vars.
        :param kwargs: assign dependent and independent variables data by name.

        Standard deviation can be provided to any variable. They have to be prefixed
        with sigma_. For example, let x be a Variable. Then sigma_x will give the
        stdev in x. Their absolute size is considered important, as is the case if
        the sigma are obtained from measurement errors.
        """
        if isinstance(model, Mapping):
            self.model = Model.from_dict(model)
        elif isinstance(model, Model):
            self.model = model
        else:
            self.model = Model(model)

        # Handle args and kwargs according to the allowed names.
        var_names = [var.name for var in self.model.vars]
        parameters = [  # Note that these are inspect.Parameter's, not symfit parameters!
            inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=1 if name.startswith('sigma_') else None)
                for name in var_names
        ]

        signature = inspect.Signature(parameters=parameters)
        bound_arguments = signature.bind(*args, **kwargs)
        # Include default values in bound_argument object
        for param in signature.parameters.values():
            if param.name not in bound_arguments.arguments:
                bound_arguments.arguments[param.name] = param.default

        self.data = copy.copy(bound_arguments.arguments)   # ordereddict of the data. Only copy the dict, not the data.
        self.sigmas = {name: self.data.pop(name) for name in var_names if name.startswith('sigma_')}

        # Replace sigmas that are one by an array of ones
        # for var, sigma in self.model.sigmas.items():
        #     print(var, sigma)
        #     if bound_arguments.arguments[sigma.name] == 1:
        #         bound_arguments.arguments[sigma.name] = np.ones(self.data[var.name].shape)

        # Wrap the variables with data using partial. The resulting function is the one to be optimised.
        self.partial_chi = functools.partial(self.model.numerical_chi, *bound_arguments.args, **bound_arguments.kwargs)
        self.partial_jacobian = [functools.partial(component, *bound_arguments.args, **bound_arguments.kwargs)
                                    for component in self.model.numerical_jacobian]

        # If user gives a preference, use that. Otherwise, use True if at least one sigma is
        # given, False if no sigma is given.
        if absolute_sigma is not None:
            self.absolute_sigma = absolute_sigma
        else:
            for name, value in self.sigmas.items():
                if value is not 1:
                    self.absolute_sigma = True
                    break
            else:
                self.absolute_sigma = False

            # self.absolute_sigma = sigma is not None

        # # set self.sigma with the relevant data.
        # if sigma:
        #     try:
        #         # sigma can be just a constant
        #         len(sigma)
        #     except TypeError:
        #         # Make it a list with the same shape as ydata
        #         sigma = np.ones_like(self.ydata) * sigma
        #     else:
        #         if y.shape != sigma.shape:
        #             raise Exception('y and sigma must have the same shape.')
        #         else:
        #             # self.sigma is an array if this else is reached, so we flatten it.
        #             sigma = sigma.reshape(-1)  # flatten
        #     finally:
        #         self.sigma = np.array(sigma)
        # else:
        #     self.sigma = 1

    @abc.abstractmethod
    def execute(self, *args, **kwargs):
        return

    # @abc.abstractmethod
    def get_initial_guesses(self):
        return np.array([param.value for param in self.model.params])

class AnalyticalFit(BaseFit):
   def execute(self, *args, **kwargs):
       """
       Analitically solve the Least Squares optimalisation.
       """
       k = sympy.symbols('k', cls=sympy.Idx)
       chi_squared_jac = jacobian(sympy.Sum(self.model.chi_squared, (k, 1, len(list(self.data.items())[0][1]))), self.model.params)
       print(self.model.chi_squared)
       print(chi_squared_jac, self.model.params)

       sol = sympy.solve(chi_squared_jac, self.model.params[1], quick=True)#, dict=True)
       return sol

class NumericalLeastSquares(BaseFit):
    """
    Solves least squares numerically using leastsqbounds. Gives results consistent with MINPACK except
    when borders are provided.
    """
    def execute(self, *args, **kwargs):
        try:
            popt, cov_x, infodic, mesg, ier = leastsqbound(
                lambda p: self.partial_chi(*p).flatten(), # This lambda unpacking is needed because scipy is an inconsistent mess.
                x0=self.get_initial_guesses(),
                bounds=self.model.bounds,
                Dfun=lambda p: np.array([component(*p).flatten() for component in self.partial_jacobian]).T,
                full_output=True,
                *args,
                **kwargs
            )
        except ValueError:
            # The Jacobian can contain nan's, causing the fit to fail. In such cases, try again without providing an exact jacobian.
            popt, cov_x, infodic, mesg, ier = leastsqbound(
                lambda p: self.partial_chi(*p).flatten(), # This lambda unpacking is needed because scipy is an inconsistent mess.
                x0=self.get_initial_guesses(),
                bounds=self.model.bounds,
                full_output=True,
                *args,
                **kwargs
            )

        if self.absolute_sigma:
            s_sq = 1
        else:
            # Rescale the covariance matrix with the residual variance
            ss_res = np.sum(infodic['fvec']**2)
            # degrees_of_freedom = len(self.data) - len(popt)
            degrees_of_freedom = len(self.data[self.model.dependent_vars[0].name]) - len(popt)

            s_sq = ss_res / degrees_of_freedom

        pcov = cov_x * s_sq if cov_x is not None else None

        self.__fit_results = FitResults(
            params=self.model.params,
            popt=popt,
            pcov=pcov,
            infodic=infodic,
            mesg=mesg,
            ier=ier,
            ydata=list(self.data.values())[0] if len(self.model.dependent_vars) == 1 else None,
            # sigma=self.sigma,
        )
        return self.__fit_results


# class AnalyticalLeastSquares(object):
#     def execute(self):
#         sol = sympy.solve(self.model.jacobian, self.model.params, dict=True)


# class BaseFit(object):
#     __metaclass__  = abc.ABCMeta
#     __jac = None  # private attribute for the jacobian
#     __fit_results = None  # private attribute for the fit_results
#     sigma = 1
#
#     def __init__(self, model, *args, **kwargs):
#         """
#         :param model: sympy expression, instance of Model, or dict.
#         :param x: xdata to fit to.  NxM
#         :param y: ydata             Nx1
#         """
#         super(BaseFit, self).__init__(*args, **kwargs)
#         if isinstance(model, Mapping):
#             self.model = Model.from_dict(model)
#         elif isinstance(model, Model):
#             self.model = model
#         else:
#             self.model = Model(model)
#
#         # Get all parameters and variables from the model.
#         # self.vars, self.params = seperate_symbols(self.model)
#         # Compile a scipy function
#         # self.scipy_func = sympy_to_scipy(self.model, self.vars, self.params)
#
#
#     # def eval_jacobian(self, p, func, x, y):
#     #     """
#     #     Evaluate the jacobian of the model.
#     #     :p: vector of parameter values
#     #     :func: scipy-type function
#     #     :x: array of x values for the evaluation of the Jacobian.
#     #     :y: ydata. Not typically used for determination of the Jacobian but provided by the calling scipy by default.
#     #     :return: vector of values of the derivatives with respect to each parameter.
#     #     """
#     #     residues = []
#     #     for jac in self.jacobian:
#     #         residue = jac(x, p)/self.sigma
#     #         # If only params in f, we must multiply with an array to preserve the shape of x
#     #         try:
#     #             len(residue)
#     #         except TypeError:  # not iterable
#     #             if len(x):
#     #                 residue *= np.ones_like(x)
#     #             else:
#     #                 residue = np.array([residue])
#     #         finally:
#     #             residues.append(residue)
#     #
#     #     return np.array(residues).T
#
#     def get_bounds(self):
#         """
#         :return: List of tuples of all bounds on parameters.
#         """
#         return [(np.nextafter(p.value, 0), p.value) if p.fixed else (p.min, p.max) for p in self.model.params]
#
#     # @property
#     # def jacobian(self):
#     #     """
#     #     Get the scipy functions for the Jacobian. This returns functions only, not values.
#     #     :return: array of derivative functions in all parameters, not values.
#     #     """
#     #     if not self.__jac:
#     #         self.__jac = []
#     #         for param in self.model.params:
#     #             # Differentiate to every param
#     #             f = sympy.diff(self.model, param)
#     #             # Make them into pythonic functions
#     #             self.__jac.append(sympy_to_scipy(f, self.vars, self.model.params))
#     #     return self.__jac
#
#     @property
#     def fit_results(self):
#         """
#         FitResults are a read-only property, because we don't want people messing with their
#         FitResults, do we?
#         :return: FitResult object if available, else None
#         """
#         return self.__fit_results if self.__fit_results else None
#
#     @abc.abstractmethod
#     def execute(self, *args, **kwargs):
#         return
#
#     @abc.abstractmethod
#     def get_initial_guesses(self):
#         return np.array([param.value for param in self.model.params])
#
#     @abc.abstractmethod
#     def error(self, p, func, x, y):
#         """
#         Error function to be minimalised. Depending on the algorithm, this can
#         return a scalar or a vector.
#         :param p: guess params
#         :param func: scipy_func to fit to
#         :param x: xdata
#         :param y: ydata
#         :return: scalar of vector.
#         """
#         return

class LeastSquares(BaseFit):
    def __init__(self, model, x, y, sigma=None, absolute_sigma=None, *args, **kwargs):
        """
        Least squares fitting. In the notation used for x and y below,
        N_1 - N_i indicate the dimension of the array inserted, and M
        the number of variables. Either the first or the last dim must
        be of size M.
        Vector-valued functions are not currently supported.

        :param model: sympy expression.
        :param x: xdata to fit to.  N_1 x ... x N_i x M
        :param y: ydata             N_1 x ... x N_i
        :param sigma: standard errors in data points. Their absolute size is
        considered important, as is the case if the sigma are obtained from
        measurement errors.
        :param absolute_sigma bool: True by default. If the sigma is only used
        for relative weights in your problem, you could consider setting it to
        False, but if your sigma are measurement errors, keep it at True.
        Note that curve_fit has this set to False by default, which is wrong in
        experimental science.
        """
        super(LeastSquares, self).__init__(model, *args, **kwargs)
        # flatten x and y to all but the final dimension.
        # Also checks if the number of variables matches the dim of x
        self.xdata, self.ydata = self._flatten(x, y)

        # If user gives a preference, use that. Otherwise, use True if sigma is
        # given, false if no sigma is given.
        if absolute_sigma is not None:
            self.absolute_sigma = absolute_sigma
        else:
            self.absolute_sigma = sigma is not None

        # set self.sigma with the relevant data.
        if sigma is not None:
            try:
                # sigma can be just a constant
                len(sigma)
            except TypeError:
                # Make it a list with the same shape as ydata
                sigma = np.ones_like(self.ydata) * sigma
            else:
                if y.shape != sigma.shape:
                    raise Exception('y and sigma must have the same shape.')
                else:
                    # self.sigma is an array if this else is reached, so we flatten it.
                    sigma = sigma.reshape(-1)  # flatten
            finally:
                self.sigma = np.array(sigma)
        else:
            self.sigma = 1

    def _flatten(self, x, y):
        """
        Flattens x up to the dimension of size len(self.vars) and y completely.
        :param x: array of shape N1 x ... x Ni x len(self.vars)
        :param y: array of shape N1 x ... x Ni
        :return: x as (N1 x ... x Ni) x len(self.vars)
                 y as (N1 x ... x Ni)
        """
        if len(self.model.vars) not in x.shape and not len(x.shape) == 1:
            raise Exception('number of vars does not match the shape of the input x data.')
        elif len(x.shape) == 1:  # this means x is already 1D.
            if x.shape != y.shape:
                raise Exception(
                    'x and y must have the same shape. x has shape {}, whereas y has shape {}.'.format(x.shape,
                                                                                                       y.shape))
            else:  # this data is already flattened.
                return x, y
        else:
            # If the last x dim is as deep as the no of vars, the remaining dimensions should match those of y.
            # Furthermore, the shapes are properly alligned.
            if x.shape[-1] == len(self.model.vars) and x.shape[:-1] == y.shape:
                x1 = x.T.reshape(len(self.model.vars), -1)  # flatten all but the dim containing the vars.
                y1 = y.T.reshape(-1)  # flatten
                return x1, y1
            # This is also acceptable, but we will have to transpose the arrays before flattening
            # for the result to make sense.
            elif x.shape[0] == len(self.model.vars) and x.shape[1:] == y.shape:
                # raise Exception(x.shape, y.shape)
                x1 = x.reshape(len(self.model.vars), -1)  # flatten all but the dim containing the vars.
                y1 = y.reshape(-1)  # flatten
                return x1, y1
            else:
                raise Exception(
                    'For multidimensional data, the first or the last dimension of xdata is expected to represent the different variables, and the shape of the remaining dimensions should match that of ydata.'
                )

    def execute(self, *args, **kwargs):
        """
        Run fitting and initiate a fit report with the result.
        :return: FitResults object
        """
        popt, cov_x, infodic, mesg, ier = leastsqbound(
            self.error,
            self.get_initial_guesses(),
            args=(self.scipy_func, self.xdata, self.ydata),
            bounds=self.get_bounds(),
            Dfun=self.eval_jacobian,
            full_output=True,
            *args,
            **kwargs
        )

        if self.absolute_sigma:
            s_sq = 1
        else:
            # Rescale the covariance matrix with the residual variance
            ss_res = np.sum(infodic['fvec']**2)
            degrees_of_freedom = len(self.ydata) - len(popt)
            s_sq = ss_res / degrees_of_freedom

        pcov = cov_x * s_sq if cov_x is not None else None

        self.__fit_results = FitResults(
            params=self.model.params,
            popt=popt,
            pcov=pcov,
            infodic=infodic,
            mesg=mesg,
            ier=ier,
            ydata=self.ydata,
            sigma=self.sigma,
        )
        return self.__fit_results

    def error(self, p, func, x, y):
        """
        :param p: param vector
        :param func: pythonic function
        :param x: xdata
        :param y: ydata
        :return: difference between the data and the fit for the given params.
        This function and eval_jacobian should have been staticmethods, but that
        way eval_jacobian does not work.
        """
        # self.sigma should be replaced by sigma as an arg!
        # More sleep is needed before I can think about this...
        return (func(x, p) - y)/self.sigma

    def get_initial_guesses(self):
        """
        Constructs a list of initial guesses from the Parameter objects.
        If no initial value is given, 1.0 is used.
        :return: list of initial guesses for self.model.params.
        """
        return super(LeastSquares, self).get_initial_guesses()

class Fit(NumericalLeastSquares):
    """
    Wrapper for LeastSquares to give it a more appealing name.
    """
    pass


class Minimize(BaseFit):
    def __init__(self, model, xdata=None, ydata=None, constraints=None, *args, **kwargs):
        """
        :model: Model to minimize
        :constraints: constraints the minimization is subject to
        :xdata:
        :ydata: data the minimization is subject to.
        """
        super(Minimize, self).__init__(model)
        self.xdata = xdata if xdata is not None else np.array([])
        self.ydata = ydata if ydata is not None else np.array([])
        self.constraints = constraints if constraints else []

    def error(self, p, func, x, y):
        if x != np.array([]) and y != np.array([]):
            return func(x, p) - y
        else:
            return func(x, p)

    def get_initial_guesses(self):
        return super(Minimize, self).get_initial_guesses()

    def execute(self, method='SLSQP', *args, **kwargs):
        ans = minimize(
            self.error,
            self.get_initial_guesses(),
            args=(self.scipy_func, self.xdata, self.ydata),
            method=method,
            # method='L-BFGS-B',
            bounds=self.get_bounds(),
            constraints = self.get_constraints(),
            jac=self.eval_jacobian,
            options={'disp': True},
        )

        # Build infodic
        infodic = {
            'fvec': ans.fun,
            'nfev': ans.nfev,
        }
        # s_sq = (infodic['fvec'] ** 2).sum() / (len(self.ydata) - len(popt))
        # pcov = cov_x * s_sq if cov_x is not None else None
        self.__fit_results = FitResults(
            params=self.model.params,
            popt=ans.x,
            pcov=None,
            infodic=infodic,
            mesg=ans.message,
            ier=ans.nit,
            ydata=self.ydata,  # Needed to calculate R^2
        )
        return self.__fit_results

    def get_constraints(self):
        """
            Turns self.constraints into a scipy compatible format.
            :return: dict of scipy compatile statements.
            """
        from sympy import Eq, Gt, Ge, Ne, Lt, Le

        cons = []
        types = {
            Eq: 'eq', Gt: 'ineq', Ge: 'ineq', Ne: 'ineq', Lt: 'ineq', Le: 'ineq'
        }

        def make_jac(constraint_lhs, p, x):
            """
            :param constraint_lhs: equation of a constraint. The lhs is assumed to be an eq, rhs a number.
            :param p: current value of the parameters to be evaluated.
            :return: numerical jacobian.
            """
            sym_jac = []
            for param in self.model.params:
                sym_jac.append(sympy.diff(constraint_lhs, param))
            ans = np.array(
                [sympy_to_scipy(jac, self.model.vars, self.model.params)(x, p) for jac in
                 sym_jac]
            )
            return ans

        for key, constraint in enumerate(self.constraints):
            # jac = make_jac(c, p)
            cons.append({
                'type': types[constraint.__class__],
                # Assume the lhs is the equation.
                'fun': lambda p, x, c: sympy_to_scipy(c.lhs, self.model.vars, self.model.params)(x, p),
                # Assume the lhs is the equation.
                'jac' : lambda p, x, c: make_jac(c.lhs, p, x),
                'args': (self.xdata, constraint)
            })
        cons = tuple(cons)
        return cons



class Maximize(Minimize):
    def error(self, p, func, x, y):
        """ Change the sign in order to maximize. """
        return - super(Maximize, self).error(p, func, x, y)

    def eval_jacobian(self, p, func, x, y):
        """ Change the sign in order to maximize. """
        return - super(Maximize, self).eval_jacobian(p, func, x, y)


class Likelihood(Maximize):
    """
    Fit using a Likelihood approach.
    """
    def __init__(self, model, xdata, *args, **kwargs):
        """
        :param model: sympy expression.
        :param x: xdata to fit to.  Nx1
        """
        super(Likelihood, self).__init__(model, xdata=xdata, *args, **kwargs)

    def execute(self, method='SLSQP', *args, **kwargs):
        # super(Likelihood, self).execute(*args, **kwargs)
        ans = minimize(
            self.error,
            self.get_initial_guesses(),
            args=(self.scipy_func, self.xdata, self.ydata),
            method=method,
            bounds=self.get_bounds(),
            constraints = self.get_constraints(),
            # jac=self.eval_jacobian, # If I find a meaning to jac I'll let you know.
            options={'disp': True},
        )

        # Build infodic
        infodic = {
            'fvec': ans.fun,
            'nfev': ans.nfev,
        }



        self.__fit_results = FitResults(
            params=self.model.params,
            popt=ans.x,
            pcov=None,
            infodic=infodic,
            mesg=ans.message,
            ier=ans.nit,
            ydata=self.ydata,  # Needed to calculate R^2
        )
        return self.__fit_results

    def get_initial_guesses(self):
        return super(Likelihood, self).get_initial_guesses()

    def error(self, p, func, x, y=None):
        """
        Error function to be maximised(!) in the case of likelihood fitting.
        :param p: guess params
        :param func: scipy_func to fit to
        :param x: xdata
        :param y: Not important for likelihood.
        :return: scalar of vector.
        """
        ans = - np.nansum(np.log(func(x, p)))
        # ans = - np.product(func(x, p)) # Why doesn't this work?
        return ans

# class LagrangeMultipliers:
#     """
#     Class to analytically solve a function subject to constraints using Karush Kuhn Tucker.
#     http://en.wikipedia.org/wiki/Karush-Kuhn-Tucker_conditions
#     """
#
#     def __init__(self, model, constraints):
#         self.model = model
#         # Seperate the constraints into equality and inequality constraint of the type <=.
#         self.equalities, self.lesser_thans = self.seperate_constraints(constraints)
#         self.model.vars, self.model.params = seperate_symbols(self.model)
#
#     @property
#     @cache
#     def lagrangian(self):
#         L = self.model
#
#         # Add equility constraints to the Lagrangian
#         for constraint, l_i in zip(self.equalities, self.l_params):
#             L += l_i * constraint
#
#         # Add inequility constraints to the Lagrangian
#         for constraint, u_i in zip(self.lesser_thans, self.u_params):
#             L += u_i * constraint
#
#         return L
#
#     @property
#     @cache
#     def l_params(self):
#         """
#         :return: Lagrange multipliers for every constraint.
#         """
#         return [Parameter(name='l_{}'.format(index)) for index in range(len(self.equalities))]
#
#     @property
#     @cache
#     def u_params(self):
#         """
#         :return: Lagrange multipliers for every inequality constraint.
#         """
#         return [Parameter(name='u_{}'.format(index)) for index in range(len(self.lesser_thans))]
#
#     @property
#     @cache
#     def all_params(self):
#         """
#         :return: All parameters. The convention is first the model parameters,
#         then lagrange multipliers for equality constraints, then inequility.
#         """
#         return self.model.params + self.l_params + self.u_params
#
#     @property
#     @cache
#     def extrema(self):
#         """
#         :return: list namedtuples of all extrema of self.model, where value = f(x1, ..., xn).
#         """
#         # Prepare the Extremum namedtuple for this number of variables.
#         field_names = [p.name for p in self.model.params] + ['value']
#         Extremum = namedtuple('Extremum', field_names)
#
#         # Calculate the function value at each solution.
#         values = [self.model.subs(sol) for sol in self.solutions]
#
#         # Build the output list of namedtuples
#         extrema_list = []
#         for value, solution in zip(values, self.solutions):
#             # Prepare an Extrumum tuple for every extremum.
#             ans = {'value': value}
#             for param in self.model.params:
#                 ans[param.name] = solution[param]
#             extrema_list.append(Extremum(**ans))
#         return extrema_list
#
#     @property
#     @cache
#     def solutions(self):
#         """
#         Do analytical optimization. This finds ALL solutions for the system.
#         Nomenclature: capital L is the Lagrangian, l the Lagrange multiplier.
#         :return: a list of dicts containing the values for all parameters,
#         including the Lagrange multipliers l_i and u_i.
#         """
#         # primal feasibility; pretend they are all equality constraints.
#         grad_L = [sympy.diff(self.lagrangian, p) for p in self.all_params]
#         solutions = sympy.solve(grad_L, self.all_params, dict=True)
#         print(grad_L, solutions, self.all_params)
#
#         if self.u_params:
#             # The smaller than constraints also have trivial solutions when u_i == 0.
#             # These are not automatically found by sympy in the previous process.
#             # Therefore we must now evaluate the gradient for these points manually.
#             u_zero = dict((u_i, 0) for u_i in self.u_params)
#             # We need to consider all combinations of u_i == 0 possible, of all lengths possible.
#             for number_of_zeros in range(1, len(u_zero) + 1):
#                 for zeros in itertools.combinations(u_zero.items(), number_of_zeros):  # zeros is a tuple of (Symbol, 0) tuples.
#                     # get a unique set of symbols.
#                     symbols = set(self.all_params) - set(symbol for symbol, _ in zeros)
#                     # differentiate w.r.t. these symbols only.
#                     relevant_grad_L = [sympy.diff(self.lagrangian, p) for p in symbols]
#
#                     solution = sympy.solve([grad.subs(zeros) for grad in relevant_grad_L], symbols, dict=True)
#                     for item in solution:
#                         item.update(zeros)  # include the zeros themselves.
#
#                     solutions += solution
#
#         return self.sanitise(solutions)
#
#     def sanitise(self, solutions):
#         """
#         Returns only solutions which are valid. This is an unfortunate consequence of the KKT method;
#         KKT parameters are not guaranteed to respect each other. However, it is easy to check this.
#         There are two things to check:
#         - all KKT parameters should be greater equal zero.
#         - all constraints should be met by the solutions.
#         :param solutions: a list of dicts, where each dict contains the coordinates of a saddle point of the lagrangian.
#         :return: bool
#         """
#         # All the inequality multipliers u_i must be greater or equal 0
#         final_solutions = []
#         for saddle_point in solutions:
#             for u_i in self.u_params:
#                 if saddle_point[u_i] < 0:
#                     break
#             else:
#                 final_solutions.append(saddle_point)
#
#         # we have to dubble check all if all our conditions are met because
#         # This is not garanteed with inequility constraints.
#         solutions = []
#         for solution in final_solutions:
#             for constraint in self.lesser_thans:
#                 test = constraint.subs(solution)
#                 if test > 0:
#                     break
#             else:
#                 solutions.append(solution)
#
#         return solutions
#
#
#
#     @staticmethod
#     def seperate_constraints(constraints):
#         """
#         We follow the definitions given here:
#         http://en.wikipedia.org/wiki/Karush-Kuhn-Tucker_conditions
#
#         IMPORTANT: <= and < are considered the same! The same goes for > and >=.
#         Strict inequalities of the type != are not currently supported.
#
#         :param constraints list: list of constraints.
#         :return: g_i are <= 0 constraints, h_j are equals 0 constraints.
#         """
#         equalities = []
#         lesser_thans = []
#         for constraint in constraints:
#             if isinstance(constraint, sympy.Eq):
#                 equalities.append(constraint.lhs - constraint.rhs)
#             elif isinstance(constraint, (sympy.Le, sympy.Lt)):
#                 lesser_thans.append(constraint.lhs - constraint.rhs)
#             elif isinstance(constraint, (sympy.Ge, sympy.Gt)):
#                 lesser_thans.append(-1 * (constraint.lhs - constraint.rhs))
#             else:
#                 raise TypeError('Constraints of type {} are not supported by this solver.'.format(type(constraint)))
#         return equalities, lesser_thans
#
# class ConstrainedFit(BaseFit):
#     """
#     Finds the analytical best fit parameters, combining data with LagrangeMultipliers
#     for the best result, if available.
#     """
#     def __init__(self, model, x, y, constraints=None, *args, **kwargs):
#         constraints = constraints if constraints is not None else []
#         value = Variable()
#         chi2 = (model - value)**2
#         self.analytic_fit = LagrangeMultipliers(chi2, constraints)
#         self.xdata = x
#         self.ydata = y
#         super(ConstrainedFit, self).__init__(chi2)
#
#     def execute(self):
#         print('here:', self.analytic_fit.solutions)
#         import inspect
#         for extremum in self.analytic_fit.extrema:
#             popt, pcov  = [], []
#             for param in self.model.params:
#                 # Retrieve the expression for this param.
#                 expr = getattr(extremum, param.name)
#                 py_expr = sympy_to_py(expr, self.model.vars, [])
#                 values = py_expr(*self.xdata)
#                 popt.append(np.average(values))
#                 pcov.append(np.var(values, ddof=len(self.model.vars)))
#             print(popt, pcov)
#
#             residuals = self.scipy_func(self.xdata, popt)
#
#             fit_results = FitResults(
#                 params=self.model.params,
#                 popt=popt,
#                 pcov=pcov,
#                 infodic={},
#                 mesg='',
#                 ier=0,
#                 r_squared=r_squared(residuals, self.ydata),
#             )
#             print(fit_results)
#         print(self.analytic_fit.extrema)
#
#     def error(self, p, func, x, y):
#         pass