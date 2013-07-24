#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    flask.ext.apify.fy
    ~~~~~~~~~~~~~~~~~~

    The extension core.

    :copyright: (c) by Vital Kudzelka
"""
from functools import wraps
from itertools import chain

from flask import g
from flask import request
from flask import Response
from flask import Blueprint
from flask import current_app
from werkzeug.local import LocalProxy
from werkzeug.datastructures import ImmutableDict

from .utils import key
from .utils import unpack_response
from .utils import self_config_value

from .exc import ApiError
from .exc import ApiNotAcceptable

from .serializers import to_json
from .serializers import to_debug
from .serializers import get_serializer
from .serializers import get_default_serializer


default_config = ImmutableDict({
    # The name of the blueprint to register API endpoints
    'blueprint_name': 'api',

    # The default mimetype returned by API endpoints
    'default_mimetype': 'application/json',

    # The name of the jinja template rendered on debug view
    'apidump_template': 'apidump.html',
})


class Apify(object):
    """The Flask extension to create an API to your application as a ninja.

    :param app: Flask application instance
    :param url_prefix: The url prefix to mount blueprint.
    :param preprocessor_funcs: A list of functions that should decorate a view
        function.
    :param finalizer_funcs: A list of functions that should be called after each
        request.
    """

    # the serializer function per mimetype
    serializers = {
        'text/html': to_debug,
        'application/json': to_json,
        'application/javascript': to_json,
    }

    def __init__(self, app=None, url_prefix=None, preprocessor_funcs=None,
        finalizer_funcs=None):

        self.url_prefix = url_prefix

        # A list of functions that should decorate original view function. To
        # register a function here, use the :meth:`preprocessor` decorator.
        self.preprocessor_funcs = list(chain((preprocess_api_response,),
                                             preprocessor_funcs or ()))

        # A list of functions that should be called after each request. To
        # register a function here, use the :meth:`finalizer` decorator.
        self.finalizer_funcs = finalizer_funcs or []

        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """Initialize an application to use with extension.

        :param app: The Flask instance

        Example::

            from flask import Flask
            from flask.ext.apify import Apify

            app = Flask(__name__)
            apify = Apify()
            apify.init_app(app)

        """
        self.app = app

        for k, v in default_config.iteritems():
            app.config.setdefault(key(k), v)

        self.blueprint = create_blueprint(self_config_value('blueprint_name', app),
                                          self.url_prefix)

        app.extensions = getattr(app, 'extensions', {})
        app.extensions['apify'] = self
        return self

    def register_routes(self):
        """Register all routes created by extension to an application.

        You MUST call this method after registration ALL view functions.

        Example::

            apify.route('/todos')(lambda: 'todos')
            apify.route('/todos/<int:todo_id>')(lambda x: 'todo %s' % x)

            # later
            apify.register_routes()

        """
        self.app.register_blueprint(self.blueprint)

    def route(self, rule, **options):
        """A decorator that is used to register a view function for a given URL
        rule, same as :meth:`route` in :class:`~flask.Blueprint` object.

        The passed view function decorates to catch all :class:`ApiError` errors
        to produce nice output on view errors.

        To allow apply decorator multiple times function will be decorated only
        if not previously decorated, e.g. has no attribute
        :attr:`is_api_method`.

        Example::

            @apify.route('/ping', defaults={'value': 200})
            @apify.route('/ping/<int:value>')
            def ping(value):
                pass

        :param rule: The URL rule string
        :param options: The options to be forwarded to the
            underlying :class:`~werkzeug.routing.Rule` object.

        Example::

            @apify.route('/todos/<int:todo_id>', methods=('DELETE'))
            def rmtodo(todo_id):
                '''Remove todo.'''
                pass

        """
        def wrapper(fn):
            if not hasattr(fn, 'is_api_method'):
                fn = catch_errors(ApiError)(fn)
                fn.is_api_method = True
            self.blueprint.add_url_rule(rule, view_func=fn, **options)
            return fn
        return wrapper

    def serializer(self, mimetype):
        """Register decorated function as serializer for specific mimetype.

        :param mimetype: The mimetype to register function as a data serializer.
        :param fn: The serializers function

        Example::

            @apify.serializer('application/xml')
            def to_xml(data):
                '''Converts data to xml.'''
                pass

        """
        def wrapper(fn):
            self.serializers[mimetype] = fn
            return fn
        return wrapper

    def preprocessor(self, fn):
        """Register a function to decorate original view function.

        :param fn: A view decorator

        Example::

            @apify.finalizer
            def login_required(fn):
                raise ApiUnauthorized()

        """
        self.preprocessor_funcs.append(fn)
        return fn

    def finalizer(self, fn):
        """Register a function to run after :class:`~flask.Response` object is
        created.

        :param fn: A function to register

        Example::

            @apify.finalizer
            def set_custom_header(res):
                res.headers['X-Rate-Limit'] = 42

        """
        self.finalizer_funcs.append(fn)
        return fn


def catch_errors(*errors):
    """The decorator to catch errors raised inside the decorated function.

    Uses in :meth:`route` of :class:`Apify` object to produce nice output for
    view errors and exceptions.

    :param errors: The errors to catch up
    :param fn: The view function to decorate

    Example::

        @catch_errors(ApiError)
        def may_raise_error():
            raise ApiError('Too busy. Try later.')

    """
    def decorator(fn):
        assert errors, 'Some dumbas forgot to specify errors to catch?'
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                # Call preprocessors
                func = apply_all(_apify.preprocessor_funcs, fn)

                # Make a response object
                res = send_api_response(func(*args, **kwargs))

                # Finalize response
                return apply_all(_apify.finalizer_funcs, res)
            except errors as exc:
                return send_api_error(exc)
        return wrapper
    return decorator


def apply_all(funcs, arg):
    """Returns the result of applying function to arg.

    :param funcs: The list of functions to apply passed argument
    :param arg: The argument passed to each function recursively
    """
    for func in funcs:
        arg = func(arg)
    return arg


def create_blueprint(name, url_prefix):
    """Creates an API blueprint, but does not register it to any specific
    application.

    :param name: The blueprint name
    :param url_prefix: The url prefix to mount blueprint.
    """
    return Blueprint(name, __name__, url_prefix=url_prefix,
                     template_folder='templates')


def preprocess_api_response(fn):
    """Preprocess response.

    Set the best possible serializer and mimetype for response to the
    application globals according with the request accept header.

    Reraise on `ApiNotAcceptable` error.
    """
    try:
        g.api_mimetype, g.api_serializer = get_serializer(guess_best_mimetype())
    except ApiNotAcceptable as exc:
        g.api_mimetype, g.api_serializer = get_default_serializer()
        raise exc
    return fn


def guess_best_mimetype():
    """Returns the best mimetype that client may accept."""
    return request.accept_mimetypes.best


def send_api_response(raw):
    """Returns the valid response object.

    :param raw: The raw data to send
    """
    raw, code, headers = unpack_response(raw)

    res = Response(g.api_serializer(raw), headers=headers, mimetype=g.api_mimetype)
    res.status_code = code
    return res


def send_api_error(exc):
    """Returns the API error wrapped in response object.

    :param exc: The exception raised
    """
    raw = {
        'error': exc.name,
        'message': exc.description,
    }
    return send_api_response((raw, exc.code))


_apify = LocalProxy(lambda: current_app.extensions['apify'])
