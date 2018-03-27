import time

try:
    from urllib.parse import parse_qs
except ImportError:
    # Python < 3
    from urlparse import parse_qs

from mock import patch

import django
from django.conf.urls import url
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_out
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.dispatch import receiver
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.client import ClientHandler

from mozilla_django_oidc.middleware import RefreshIDToken
from mozilla_django_oidc.urls import urlpatterns as orig_urlpatterns


User = get_user_model()


DJANGO_VERSION = tuple(django.VERSION[0:2])


class RefreshIDTokenMiddlewareTestCase(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = RefreshIDToken()
        self.user = User.objects.create_user('example_username')

    def test_anonymous(self):
        request = self.factory.get('/foo')
        request.user = AnonymousUser()
        response = self.middleware.process_request(request)
        self.assertTrue(not response)

    def test_is_oidc_path(self):
        request = self.factory.get('/oidc/callback/')
        request.user = AnonymousUser()
        response = self.middleware.process_request(request)
        self.assertTrue(not response)

    def test_is_POST(self):
        request = self.factory.post('/foo')
        request.user = AnonymousUser()
        response = self.middleware.process_request(request)
        self.assertTrue(not response)

    def test_is_ajax(self):
        request = self.factory.get(
            '/foo',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        request.user = self.user

        response = self.middleware.process_request(request)
        self.assertTrue(not response)

    @override_settings(OIDC_OP_AUTHORIZATION_ENDPOINT='http://example.com/authorize')
    @override_settings(OIDC_RP_CLIENT_ID='foo')
    @override_settings(OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS=120)
    @patch('mozilla_django_oidc.middleware.get_random_string')
    def test_no_oidc_token_expiration_forces_renewal(self, mock_random_string):
        mock_random_string.return_value = 'examplestring'

        request = self.factory.get('/foo')
        request.user = self.user
        request.session = {}

        response = self.middleware.process_request(request)

        self.assertEquals(response.status_code, 302)
        url, qs = response.url.split('?')
        self.assertEquals(url, 'http://example.com/authorize')
        expected_query = {
            'response_type': ['code'],
            'redirect_uri': ['http://testserver/callback/'],
            'client_id': ['foo'],
            'nonce': ['examplestring'],
            'prompt': ['none'],
            'scope': ['openid email'],
            'state': ['examplestring'],
        }
        self.assertEquals(expected_query, parse_qs(qs))

    @override_settings(OIDC_OP_AUTHORIZATION_ENDPOINT='http://example.com/authorize')
    @override_settings(OIDC_RP_CLIENT_ID='foo')
    @override_settings(OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS=120)
    @patch('mozilla_django_oidc.middleware.get_random_string')
    def test_expired_token_forces_renewal(self, mock_random_string):
        mock_random_string.return_value = 'examplestring'

        request = self.factory.get('/foo')
        request.user = self.user
        request.session = {
            'oidc_id_token_expiration': time.time() - 10
        }

        response = self.middleware.process_request(request)

        self.assertEquals(response.status_code, 302)
        url, qs = response.url.split('?')
        self.assertEquals(url, 'http://example.com/authorize')
        expected_query = {
            'response_type': ['code'],
            'redirect_uri': ['http://testserver/callback/'],
            'client_id': ['foo'],
            'nonce': ['examplestring'],
            'prompt': ['none'],
            'scope': ['openid email'],
            'state': ['examplestring'],
        }
        self.assertEquals(expected_query, parse_qs(qs))


# This adds a "home page" we can test against.
def fakeview(req):
    return HttpResponse('Win!')


urlpatterns = list(orig_urlpatterns) + [
    url(r'^mdo_fake_view/$', fakeview, name='mdo_fake_view')
]


def override_middleware(fun):
    classes = [
        'django.contrib.sessions.middleware.SessionMiddleware',
        'mozilla_django_oidc.middleware.RefreshIDToken',
    ]
    if DJANGO_VERSION >= (1, 10):
        return override_settings(MIDDLEWARE=classes)(fun)
    return override_settings(MIDDLEWARE_CLASSES=classes)(fun)


class UserifiedClientHandler(ClientHandler):
    """Enhances ClientHandler to "work" with users properly"""
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user')
        super(UserifiedClientHandler, self).__init__(*args, **kwargs)

    def get_response(self, req):
        req.user = self.user
        return super(UserifiedClientHandler, self).get_response(req)


class ClientWithUser(Client):
    """Enhances Client to "work" with users properly"""
    def __init__(self, enforce_csrf_checks=False, **defaults):
        # Start off with the AnonymousUser
        self.user = AnonymousUser()
        # Get this because we need to create a new UserifiedClientHandler later
        self.enforce_csrf_checks = enforce_csrf_checks
        super(ClientWithUser, self).__init__(**defaults)
        # Stomp on the ClientHandler with one that correctly makes request.user
        # the AnonymousUser
        self.handler = UserifiedClientHandler(enforce_csrf_checks, user=self.user)

    def login(self, **credentials):
        from django.contrib.auth import authenticate

        # Try to authenticate and throw an exception if that fails; also, this gets
        # the user instance that was authenticated with
        user = authenticate(**credentials)
        if not user:
            # Client lets you fail authentication without providing any helpful
            # messages; we throw an exception because silent failure is
            # unhelpful
            raise Exception('Unable to authenticate with %r' % credentials)

        ret = super(ClientWithUser, self).login(**credentials)
        if not ret:
            raise Exception('Login failed')

        # Stash the user object it used and rebuild the UserifiedClientHandler
        self.user = user
        self.handler = UserifiedClientHandler(self.enforce_csrf_checks, user=self.user)
        return ret


@override_settings(ROOT_URLCONF='tests.test_middleware')
@override_middleware
class MiddlewareTestCase(TestCase):
    """These tests test the middleware as part of the request/response cycle"""
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='example_username', password='password')
        cache.clear()

    @override_settings(OIDC_EXEMPT_URLS=['mdo_fake_view'])
    def test_get_exempt_urls_setting_view_name(self):
        middleware = RefreshIDToken()
        self.assertEquals(
            sorted(list(middleware.exempt_urls)),
            [u'/authenticate/', u'/callback/', u'/logout/', u'/mdo_fake_view/']
        )

    @override_settings(OIDC_EXEMPT_URLS=['/foo/'])
    def test_get_exempt_urls_setting_url_path(self):
        middleware = RefreshIDToken()
        self.assertEquals(
            sorted(list(middleware.exempt_urls)),
            [u'/authenticate/', u'/callback/', u'/foo/', u'/logout/']
        )

    def test_anonymous(self):
        client = ClientWithUser()
        resp = client.get('/mdo_fake_view/')
        self.assertEqual(resp.status_code, 200)

    @override_settings(OIDC_OP_AUTHORIZATION_ENDPOINT='http://example.com/authorize')
    @override_settings(OIDC_RP_CLIENT_ID='foo')
    @override_settings(OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS=120)
    def test_authenticated_user(self):
        client = ClientWithUser()
        client.login(username=self.user.username, password='password')

        # Set the expiration to some time in the future so this user is valid
        session = client.session
        session['oidc_id_token_expiration'] = time.time() + 100
        session.save()

        resp = client.get('/mdo_fake_view/')
        self.assertEqual(resp.status_code, 200)

    @override_settings(OIDC_OP_AUTHORIZATION_ENDPOINT='http://example.com/authorize')
    @override_settings(OIDC_RP_CLIENT_ID='foo')
    @override_settings(OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS=120)
    @patch('mozilla_django_oidc.middleware.get_random_string')
    def test_expired_token_redirects_to_sso(self, mock_random_string):
        mock_random_string.return_value = 'examplestring'

        client = ClientWithUser()
        client.login(username=self.user.username, password='password')

        # Set expiration to some time in the past
        session = client.session
        session['oidc_id_token_expiration'] = time.time() - 100
        session.save()

        resp = client.get('/mdo_fake_view/')
        self.assertEqual(resp.status_code, 302)

        url, qs = resp.url.split('?')
        self.assertEquals(url, 'http://example.com/authorize')
        expected_query = {
            'response_type': ['code'],
            'redirect_uri': ['http://testserver/callback/'],
            'client_id': ['foo'],
            'nonce': ['examplestring'],
            'prompt': ['none'],
            'scope': ['openid email'],
            'state': ['examplestring'],
        }
        self.assertEquals(expected_query, parse_qs(qs))

    @override_settings(OIDC_OP_AUTHORIZATION_ENDPOINT='http://example.com/authorize')
    @override_settings(OIDC_RP_CLIENT_ID='foo')
    @override_settings(OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS=120)
    @patch('mozilla_django_oidc.middleware.get_random_string')
    def test_refresh_fails_for_already_signed_in_user(self, mock_random_string):
        mock_random_string.return_value = 'examplestring'

        # Mutable to log which users get logged out.
        logged_out_users = []

        # Register a signal on 'user_logged_out' so we can
        # update 'logged_out_users'.
        @receiver(user_logged_out)
        def logged_out(sender, user=None, **kwargs):
            logged_out_users.append(user)

        client = ClientWithUser()
        # First confirm that the home page is a public page.
        resp = client.get('/')
        # At least security doesn't kick you out.
        self.assertEquals(resp.status_code, 404)
        # Also check that this page doesn't force you to redirect
        # to authenticate.
        resp = client.get('/mdo_fake_view/')
        self.assertEquals(resp.status_code, 200)
        client.login(username=self.user.username, password='password')

        # Set expiration to some time in the past
        session = client.session
        session['oidc_id_token_expiration'] = time.time() - 100
        session.save()

        # Confirm that now you're forced to authenticate again.
        resp = client.get('/mdo_fake_view/')
        self.assertEquals(resp.status_code, 302)
        self.assertTrue(
            'http://example.com/authorize' in resp.url and
            'prompt=none' in resp.url
        )
        # Now suppose the user goes there and something goes wrong.
        # For example, the user might have become "blocked" or the 2FA
        # verficiation has expired and needs to be done again.
        resp = client.get('/callback/', {
            'error': 'login_required',
            'error_description': 'Multifactor authentication required',
        })
        self.assertEqual(resp.status_code, 302)
        # Note, in versions of Django <=1.8, this 'resp.url' will be
        # an absolute URL, so we need to make this split to make sure the
        # test suite works in old and new versions of Django.
        if 'http://testserver' in resp.url:
            self.assertEquals(resp.url, 'http://testserver/')
        else:
            self.assertEquals(resp.url, '/')

        # Since the user in 'client' doesn't change, we have to use other
        # queues to assert that the user got logged out properly.

        # The session gets flushed when you get signed out.
        # This is the only decent way to know the user lost all
        # request.session and
        self.assertTrue(not client.session.items())

        # The signal we registered should have fired for this user.
        self.assertEquals(client.user, logged_out_users[0])
