#!/usr/bin/env python
# -*- coding: utf-8 -*-
import pytest

from flask.ext.apify.fy import set_best_serializer
from flask.ext.apify.exc import ApiError
from flask.ext.apify.exc import ApiUnauthorized
from flask.ext.apify.exc import ApiNotAcceptable
from flask.ext.apify.serializers import to_json
from flask.ext.apify.serializers import to_debug
from flask.ext.apify.serializers import get_serializer
from flask.ext.apify.serializers import get_default_serializer


@pytest.fixture(params=['application/json', 'application/javascript', 'text/html'])
def mimetype(request):
    return request.param


@pytest.fixture
def accept_mimetypes(mimetype):
    return [('Accept', mimetype)]


@pytest.fixture
def accept_json():
    return accept_mimetypes('application/json')


def test_apify_init(webapp, apify):
    assert 'apify' in webapp.extensions
    assert apify.finalizer_funcs == []
    assert apify.serializers['text/html'] is to_debug
    assert apify.serializers['application/json'] is to_json
    assert apify.serializers['application/javascript'] is to_json


def test_apify_register_serializer_for_mimetype(webapp, apify):
    fn = lambda x: x
    apify.serializer('application/xml')(fn)

    with webapp.test_request_context():
        mimetype, serializer = get_serializer('application/xml')
        assert mimetype == 'application/xml'
        assert serializer is fn


def add_api_rule(apify,
                  fn=lambda: dict(status='api call done'),
                  endpoint='/wtf',
                  dofinalize=True,
                  **options):
    """Register new route via apify.

    :param apify: The Apify extension instance
    :param fn: The view function to register
    :param endpoint: The url rule
    :param dofinalize: Finalize API creation
    :param options: The options passed to :meth:`route` of :class:`Apify`
        instance
    """
    apify.route(endpoint, **options)(fn)
    if dofinalize:
        apifinalize(apify)


get_mimetype = lambda x: x[0][1]
"""Returns mimetype from tuple generated by :func:`accept_mimetypes`."""


apifinalize = lambda apify: apify.register_routes()
"""Finalize api creation, e.g. register blueprint to the application."""


def test_apify_get_serializer(webapp, mimetype):
    with webapp.test_request_context():
        mime, fn = get_serializer(mimetype)
        assert mime == mimetype
        assert callable(fn)


def test_apify_get_serializer_may_raise_error(webapp):
    with webapp.test_request_context():
        with pytest.raises(ApiNotAcceptable):
            get_serializer('nosuch/mimetype')


def test_apify_default_response_mimetype_is_application_json(webapp):
    with webapp.test_request_context():
        mimetype, fn = get_default_serializer()
        assert mimetype == 'application/json'
        assert callable(fn)


def test_apify_get_default_serializer_may_raise_error_if_nosuch_serializer(webapp):
    webapp.config['APIFY_DEFAULT_MIMETYPE'] = 'nosuch/mimetype'

    with webapp.test_request_context():
        with pytest.raises(RuntimeError):
            get_default_serializer()


def test_apify_route(apify, client, accept_mimetypes):
    add_api_rule(apify)

    res = client.get('/wtf', headers=accept_mimetypes)
    assert res.status == '200 OK'
    assert res.mimetype == get_mimetype(accept_mimetypes)
    assert 'api call done' in res.data


def test_apify_call_require_explicit_mimetype(apify, client):
    add_api_rule(apify)

    res = client.get('/wtf')
    assert res.status == '406 NOT ACCEPTABLE'
    assert res.mimetype == 'application/json'


def test_apify_handle_custom_errors(apify, client, accept_mimetypes):
    class ImATeapot(ApiError):
        code = 418
        description = 'This server is a teapot, not a coffee machine'

    def fn(): raise ImATeapot()
    add_api_rule(apify, fn, '/teapot')

    res = client.get('/teapot', headers=accept_mimetypes)
    assert res.status_code == 418
    assert 'This server is a teapot, not a coffee machine' in res.data


def test_apify_allow_apply_route_decorator_multiple_times(apify, client, accept_json):
    @apify.route('/ping', defaults={'value': 200})
    @apify.route('/ping/<int:value>')
    def ping(value):
        return {'value': value}
    apifinalize(apify)

    res = client.get('/ping', headers=accept_json)
    assert res.status == '200 OK'
    assert '{"value": 200}' == res.data

    res = client.get('/ping/404', headers=accept_json)
    assert res.status == '200 OK'
    assert '{"value": 404}' == res.data


def test_apify_add_preprocessor(apify):
    fn = lambda x: x
    apify.preprocessor(fn)

    assert apify.preprocessor_funcs == [set_best_serializer, fn]


def test_apify_exec_preprocessors(apify, client, accept_mimetypes):
    add_api_rule(apify)

    @apify.preprocessor
    def login_required(fn):
        raise ApiUnauthorized()

    res = client.get('/wtf', headers=accept_mimetypes)
    assert res.status == '401 UNAUTHORIZED'
    assert "The server could not verify that you are authorized to access the requested URL." in res.data


def test_apify_add_finalizer(apify):
    fn = lambda x: x
    apify.finalizer(fn)

    assert fn in apify.finalizer_funcs


def test_apify_exec_finalizer(apify, client, accept_mimetypes):
    add_api_rule(apify)

    @apify.finalizer
    def set_custom_header(res):
        res.headers['X-Rate-Limit'] = 42
        return res

    res = client.get('/wtf', headers=accept_mimetypes)
    assert res.status == '200 OK'
    assert res.headers['X-Rate-Limit'] == '42'


def test_apify_can_handle_finalizer_error(apify, client, accept_mimetypes):
    add_api_rule(apify)

    class ImATeapot(ApiError):
        code = 418

    def fn(res): raise ImATeapot('Server too hot. Try it later.')
    apify.finalizer(fn)

    res = client.get('/wtf', headers=accept_mimetypes)
    assert res.status == '418 I\'M A TEAPOT'
    assert 'Server too hot. Try it later.' in res.data
