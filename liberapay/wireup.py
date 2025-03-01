from decimal import Decimal
import json
import logging
from operator import itemgetter
import os
import re
import socket
import traceback

import babel.localedata
from babel.messages.pofile import read_po
from babel.numbers import parse_pattern
import boto3
from mailshake import AmazonSESMailer, ToConsoleMailer, SMTPMailer
import pando
from postgres.cursors import SimpleRowCursor
import psycopg2
from psycopg2.extensions import adapt, AsIs, new_type, register_adapter, register_type
from psycopg2_pool import PoolError
import sass
import sentry_sdk
from state_chain import StateChain

from liberapay import elsewhere
import liberapay.billing.payday
from liberapay.exceptions import NeedDatabase
from liberapay.i18n.base import (
    ALIASES, ALIASES_R, COUNTRIES, LANGUAGES_2, LOCALES, Locale, make_sorted_dict
)
from liberapay.i18n.currencies import Money, MoneyBasket, get_currency_exchange_rates
from liberapay.i18n.plural_rules import get_function_from_rule
from liberapay.models import DB
from liberapay.models.account_elsewhere import _AccountElsewhere, AccountElsewhere
from liberapay.models.community import _Community, Community
from liberapay.models.encrypted import Encrypted
from liberapay.models.exchange_route import ExchangeRoute
from liberapay.models.participant import Participant
from liberapay.models.payin import Payin
from liberapay.models.repository import Repository
from liberapay.models.tip import Tip
from liberapay.security.crypto import Cryptograph
from liberapay.utils import find_files, markdown, resolve
from liberapay.utils.emails import compile_email_spt
from liberapay.utils.http_caching import asset_etag
from liberapay.utils.types import Object
from liberapay.version import get_version
from liberapay.website import Website


def canonical(env):
    canonical_scheme = env.canonical_scheme
    canonical_host = env.canonical_host
    cookie_domain = None
    if canonical_host:
        canonical_url = '%s://%s' % (canonical_scheme, canonical_host)
        if ':' not in canonical_host:
            cookie_domain = '.' + canonical_host
    else:
        canonical_url = ''
    asset_url = canonical_url+'/assets/'
    return locals()


class CSP(bytes):

    # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy/default-src
    based_on_default_src = set(b'''
        child-src connect-src font-src frame-src img-src manifest-src
        media-src object-src script-src style-src worker-src
    '''.split())

    def __new__(cls, x):
        if isinstance(x, dict):
            self = bytes.__new__(cls, b';'.join(b' '.join(t).rstrip() for t in x.items()) + b';')
            self.directives = dict(x)
        else:
            self = bytes.__new__(cls, x)
            self.directives = dict(
                (d.split(b' ', 1) + [b''])[:2] for d in self.split(b';') if d
            )
        return self

    def allow(self, directive, value):
        d = dict(self.directives)
        old_value = d.get(directive)
        if old_value is None and directive in self.based_on_default_src:
            old_value = d.get(b'default-src')
        d[directive] = b'%s %s' % (old_value, value) if old_value else value
        return CSP(d)


def csp(canonical_host, canonical_scheme, env):
    csp = (
        b"default-src 'self' %(main_domain)s;"
        b"connect-src 'self' *.liberapay.org;"
        b"form-action 'self';"
        b"img-src * blob: data:;"
        b"object-src 'none';"
    ) % {b'main_domain': canonical_host.encode('ascii')}
    csp += env.csp_extra.encode()
    if canonical_scheme == 'https':
        csp += b"upgrade-insecure-requests;"
    return {'csp': CSP(csp)}


def crypto():
    return {'cryptograph': Cryptograph()}


class NoDB:

    def __getattr__(self, attr):
        raise NeedDatabase()

    __bool__ = lambda self: False

    back_as_registry = {}

    def register_model(self, model):
        model.db = self


def database(env, tell_sentry):
    dburl = env.database_url
    maxconn = env.database_maxconn
    try:
        db = DB(dburl, maxconn=maxconn, cursor_factory=SimpleRowCursor)
    except psycopg2.OperationalError as e:
        tell_sentry(e, allow_reraise=False)
        db = NoDB()

    itemgetter0 = itemgetter(0)

    def back_as_Object(cols, vals):
        return Object(zip(map(itemgetter0, cols), vals))

    db.back_as_registry[Object] = db.back_as_registry['Object'] = back_as_Object

    models = (
        _AccountElsewhere, AccountElsewhere, _Community, Community,
        Encrypted, ExchangeRoute, Participant, Payin, Repository, Tip,
    )
    for model in models:
        db.register_model(model)
        setattr(db, model.__name__, model)
    liberapay.billing.payday.Payday.db = db

    def adapt_set(s):
        return adapt(tuple(s))
    register_adapter(set, adapt_set)

    def adapt_money(m):
        return AsIs('(%s,%s)::currency_amount' % (adapt(m.amount), adapt(m.currency)))
    register_adapter(Money, adapt_money)

    def cast_currency_amount(v, cursor):
        return None if v in (None, '(,)') else Money(*v[1:-1].split(','))
    try:
        oid = db.one("SELECT 'currency_amount'::regtype::oid")
        register_type(new_type((oid,), 'currency_amount', cast_currency_amount))
    except (psycopg2.ProgrammingError, NeedDatabase):
        pass

    def adapt_money_basket(b):
        return AsIs(
            "_wrap_amounts('%s'::jsonb)" %
            json.dumps({k: str(v) for k, v in b.amounts.items() if v}).replace("'", "''")
        )
    register_adapter(MoneyBasket, adapt_money_basket)

    def cast_currency_basket(v, cursor):
        if v is None:
            return None
        parts = v[1:-1].split(',', 2)
        if len(parts) == 2:
            eur, usd = parts
            obj = None
        else:
            eur, usd, obj = parts
        if obj:
            amounts = json.loads(obj[1:-1].replace('""', '"') if obj[0] == '"' else obj)
            amounts = {k: Decimal(str(v)) for k, v in amounts.items()}
        else:
            amounts = {}
            if eur:
                amounts['EUR'] = Decimal(eur)
            if usd:
                amounts['USD'] = Decimal(usd)
        return MoneyBasket(**amounts)
    try:
        oid = db.one("SELECT 'currency_basket'::regtype::oid")
        register_type(new_type((oid,), 'currency_basket', cast_currency_basket))
    except (psycopg2.ProgrammingError, NeedDatabase):
        pass

    if db and env.override_query_cache:
        db.cache.max_size = 0

    return {'db': db}


class AppConf:

    fields = dict(
        app_name=str,
        bitbucket_callback=str,
        bitbucket_id=str,
        bitbucket_secret=str,
        bot_github_token=str,
        bot_github_username=str,
        check_avatar_urls=bool,
        check_email_domains=bool,
        check_email_servers=bool,
        cron_intervals=dict,
        facebook_callback=str,
        facebook_id=str,
        facebook_secret=str,
        github_callback=str,
        github_id=str,
        github_secret=str,
        gitlab_callback=str,
        gitlab_id=str,
        gitlab_secret=str,
        google_callback=str,
        google_id=str,
        google_secret=str,
        linuxfr_callback=str,
        linuxfr_id=str,
        linuxfr_secret=str,
        log_emails=bool,
        mangopay_base_url=str,
        mangopay_client_id=str,
        mangopay_client_password=str,
        openstreetmap_api_url=str,
        openstreetmap_auth_url=str,
        openstreetmap_callback=str,
        openstreetmap_id=str,
        openstreetmap_secret=str,
        password_rounds=int,
        payday_label=str,
        payday_repo=str,
        payin_methods=dict,
        paypal_domain=str,
        paypal_id=str,
        paypal_secret=str,
        s3_endpoint=str,
        s3_public_access_key=str,
        s3_secret_key=str,
        s3_region=str,
        s3_payday_logs_bucket=str,
        ses_feedback_queue_url=str,
        ses_region=str,
        sepa_creditor_identifier=str,
        show_sandbox_warning=bool,
        socket_timeout=float,
        smtp_host=str,
        smtp_port=int,
        smtp_username=str,
        smtp_password=str,
        smtp_use_tls=bool,
        stripe_callback_secret=str,
        stripe_connect_callback_secret=str,
        stripe_connect_id=str,
        stripe_publishable_key=str,
        stripe_secret_key=str,
        twitch_id=str,
        twitch_secret=str,
        twitter_callback=str,
        twitter_id=str,
        twitter_secret=str,
    )

    def __init__(self, d):
        d = d if isinstance(d, dict) else dict(d)

        unexpected = set(d) - set(self.fields)
        if unexpected:
            print("Found %i unexpected variables in the app_conf table:  %s" %
                  (len(unexpected), ' '.join(unexpected)))

        missing, mistyped = [], []
        for k, t in self.fields.items():
            if k in d:
                v = d[k]
                if isinstance(v, t):
                    self.__dict__[k] = v
                else:
                    mistyped.append((k, v, t))
            else:
                missing.append(k)
        if missing:
            print('Missing configuration variables: ', ' '.join(missing))
        for k, v, t in mistyped:
            print('Invalid configuration variable, %s: %s is of type %s, not %s' %
                  (k, json.dumps(v), type(v), t))

        self.missing = missing
        self.mistyped = mistyped
        self.unexpected = unexpected


def app_conf(db):
    if not db:
        return {'app_conf': None}
    app_conf = AppConf(db.all("SELECT key, value FROM app_conf"))
    if app_conf:
        socket.setdefaulttimeout(app_conf.socket_timeout)
    return {'app_conf': app_conf}


def mail(app_conf, env, project_root='.'):
    if not app_conf:
        return
    smtp_conf = {
        k[5:]: v for k, v in app_conf.__dict__.items() if k.startswith('smtp_')
    }
    if smtp_conf:
        smtp_conf.setdefault('timeout', app_conf.socket_timeout)
    if getattr(app_conf, 'ses_region', None):
        mailer = AmazonSESMailer(
            env.aws_access_key_id, env.aws_secret_access_key,
            region_name=app_conf.ses_region
        )
    elif smtp_conf:
        mailer = SMTPMailer(**smtp_conf)
    else:
        mailer = ToConsoleMailer()
    emails = {}
    emails_dir = project_root+'/emails/'
    i = len(emails_dir)
    for spt in find_files(emails_dir, '*.spt'):
        base_name = spt[i:-4]
        emails[base_name] = compile_email_spt(spt)

    def log_email(message):
        message = dict(message)
        html, text = message.pop('html'), message.pop('text')
        print('\n', ' ', '='*26, 'BEGIN EMAIL', '='*26)
        print(json.dumps(message))
        print('[---] text/html')
        print(html)
        print('[---] text/plain')
        print(text)
        print('  ', '='*27, 'END EMAIL', '='*27)

    if app_conf.log_emails and not isinstance(mailer, ToConsoleMailer):
        log_email = log_email
    else:
        log_email = lambda *a, **kw: None

    return {'emails': emails, 'log_email': log_email, 'mailer': mailer}


def billing(app_conf):
    if not app_conf:
        return
    import mangopay
    sandbox = 'sandbox' in app_conf.mangopay_base_url
    mangopay.sandbox = sandbox
    handler = mangopay.APIRequest(
        client_id=app_conf.mangopay_client_id,
        passphrase=app_conf.mangopay_client_password,
        sandbox=sandbox,
        timeout=app_conf.socket_timeout,
    )
    mangopay.get_default_handler = mangopay.base.get_default_handler = \
        mangopay.query.get_default_handler = lambda: handler

    # https://github.com/Mangopay/mangopay2-python-sdk/issues/95
    if not sandbox:
        mangopay.api.logger.setLevel(logging.CRITICAL)

    # https://github.com/Mangopay/mangopay2-python-sdk/issues/118
    mangopay.resources.LegalUser.person_type = 'LEGAL'

    # https://github.com/Mangopay/mangopay2-python-sdk/issues/144
    import liberapay.billing.watcher
    mangopay.signals.request_finished.connect(liberapay.billing.watcher.on_response)

    # https://github.com/Mangopay/mangopay2-python-sdk/issues/157
    cls = mangopay.resources.DirectPayIn
    field = mangopay.fields.Field(api_name='Billing')
    field.add_to_class(cls, 'billing')
    cls._meta.api_names[field.api_name] = field.name


def stripe(app_conf):
    if not app_conf:
        return
    import stripe
    stripe.api_key = app_conf.stripe_secret_key
    stripe.api_version = '2019-08-14'
    stripe.client_id = app_conf.stripe_connect_id
    stripe.max_network_retries = 2


def username_restrictions(www_root):
    return {'restricted_usernames': os.listdir(www_root)}


def version(env):
    try:
        version = get_version()
    except Exception:
        if env.instance_type == 'production':
            raise
        version = None
    return {'version': version}


def make_sentry_teller(env, version):
    if env.sentry_dsn:
        sentry_sdk.init(
            env.sentry_dsn,
            environment=env.instance_type,
            release=version,
            debug=False,  # Pass `True` when investigating an integration issue
        )
        sentry = True
    else:
        sentry = False
        print("Won't log to Sentry (SENTRY_DSN is empty).")

    def tell_sentry(exception, send_state=True, allow_reraise=True, level=None):
        r = {'sentry_ident': None}

        if isinstance(exception, pando.Response):
            if exception.code < 500:
                # Only log server errors
                return r
            if not level and exception.code in (502, 504):
                # This kind of error is usually transient and not our fault.
                level = 'warning'

        if isinstance(exception, NeedDatabase):
            # Don't flood Sentry when DB is down
            return r

        state = Website.state.get(None) or {}
        if isinstance(exception, PoolError):
            # If this happens, then the `DATABASE_MAXCONN` value is too low.
            state['exception'] = NeedDatabase()

        if isinstance(exception, psycopg2.Error):
            from liberapay.website import website
            if getattr(website, 'db', None):
                try:
                    website.db.one('SELECT 1 AS x')
                except psycopg2.Error as e:
                    # If it can't answer this simple query, then it's either
                    # down or unreachable. Show the proper 503 error page.
                    website.db.okay = False
                    state['exception'] = NeedDatabase()
                    if sentry:
                        # Record the exception raised above instead of the
                        # original one, to avoid duplicate issues.
                        return tell_sentry(e, state, allow_reraise=True)

                if 'read-only' in str(exception):
                    # DB is in read only mode
                    state['db_is_readonly'] = True
                    # Show the proper 503 error page
                    state['exception'] = NeedDatabase()
                    # Don't reraise this in tests
                    allow_reraise = False

        if isinstance(exception, ValueError):
            if 'cannot contain NUL (0x00) characters' in str(exception):
                # https://github.com/liberapay/liberapay.com/issues/675
                response = state.get('response') or pando.Response()
                response.code = 400
                response.body = str(exception)
                r['exception'] = None
                r['response'] = response
                return r

        if not sentry:
            # No Sentry, log to stderr instead
            traceback.print_exc()
            # Reraise if allowed
            if env.sentry_reraise and allow_reraise:
                raise
            return r

        # Prepare context data
        if not level:
            level = 'warning' if isinstance(exception, Warning) else 'error'
        scope_dict = {'level': level}
        if state:
            try:
                # https://docs.sentry.io/platforms/python/enriching-events/identify-user/
                user_data = scope_dict['user'] = {}
                user = state.get('user')
                if isinstance(user, Participant):
                    user_data['id'] = getattr(user, 'id', None)
                    user_data['username'] = getattr(user, 'username', None)
                # https://develop.sentry.dev/sdk/event-payloads/request/
                request = state.get('request')
                if request is not None:
                    user_data['ip_address'] = str(request.source)
                    decode = lambda b: b.decode('ascii', 'backslashreplace')
                    scope_dict['contexts'] = {}
                    scope_dict['contexts']['request'] = {
                        'method': request.method,
                        'url': request.line.uri.decoded,
                        'headers': {
                            decode(k): decode(b', '.join(v))
                            for k, v in request.headers.items()
                            if k != b'Cookie'
                        },
                    }
                # https://docs.sentry.io/platforms/python/enriching-events/tags/
                scope_dict['tags'] = {
                    'lang': getattr(state.get('locale'), 'language', None),
                }
            except Exception as e:
                tell_sentry(e, send_state=False)

        # Tell Sentry
        r['sentry_ident'] = sentry_sdk.capture_exception(exception, **scope_dict)
        return r

    return {'tell_sentry': tell_sentry}


class PlatformRegistry:
    """Registry of platforms we support.
    """

    def __init__(self, platforms):
        self.list = platforms
        self.dict = dict((p.name, p) for p in platforms)
        self.__dict__.update(self.dict)
        self.order = dict((p.name, i) for i, p in enumerate(platforms))
        self._hasattr_cache = {}

    def __contains__(self, platform):
        return platform.name in self.dict

    def __iter__(self):
        return iter(self.list)

    def __len__(self):
        return len(self.list)

    def _cache_hasattr(self, attr):
        r = PlatformRegistry([p for p in self if getattr(p, attr, None)])
        self._hasattr_cache[attr] = r
        return r

    def get(self, k, default=None):
        return self.dict.get(k, default)

    def hasattr(self, attr):
        r = self._hasattr_cache.get(attr)
        return r or self._cache_hasattr(attr)

    def index(self, name):
        return self.order[name]


def accounts_elsewhere(app_conf, asset, canonical_url, db):
    if not app_conf:
        return {'platforms': db, 'follow_platforms': db}
    platforms = []
    for cls in elsewhere.CLASSES:
        conf = {
            k[len(cls.name)+1:]: v
            for k, v in app_conf.__dict__.items() if k.startswith(cls.name+'_')
        }
        conf.setdefault('api_timeout', app_conf.socket_timeout)
        conf.setdefault('app_name', app_conf.app_name)
        conf.setdefault('app_url', canonical_url)
        if hasattr(cls, 'register_app'):
            callback_url = canonical_url + '/on/' + cls.name + ':{domain}/associate'
            platforms.append(cls(None, None, callback_url, **conf))
        elif hasattr(cls, 'based_on'):
            based_on = cls.based_on
            callback_url = canonical_url + '/on/' + cls.name + '/associate'
            platforms.append(cls(
                getattr(app_conf, based_on + '_id'),
                getattr(app_conf, based_on + '_secret'),
                callback_url,
                **conf
            ))
        else:
            platforms.append(cls(
                conf.pop('id'),
                conf.pop('secret'),
                conf.pop('callback', canonical_url + '/on/' + cls.name + '/associate'),
                **conf
            ))

    platforms = [p for p in platforms if p.api_secret or hasattr(p, 'register_app')]
    order = db.all("""
        SELECT platform
          FROM (
            SELECT e.platform, count(*) as c
              FROM elsewhere e
              JOIN participants p ON p.id = e.participant
             WHERE p.status = 'active'
               AND p.hide_from_lists = 0
          GROUP BY e.platform
               ) a
      ORDER BY c DESC, platform ASC
    """)
    n = len(order)
    order = dict(zip(order, range(n)))
    platforms = sorted(platforms, key=lambda p: (order.get(p.name, n), p.name))
    platforms = PlatformRegistry(platforms)

    follow_platforms = [p for p in platforms if getattr(p, 'api_follows_path', None)]
    follow_platforms = PlatformRegistry(follow_platforms)

    for platform in platforms:
        if platform.fontawesome_name:
            continue
        platform.icon = asset(
            'platforms/%s.svg' % platform.name,
            'platforms/%s.16.png' % platform.name,
        )
        platform.logo = asset(
            'platforms/%s.svg' % platform.name,
            'platforms/%s.png' % platform.name,
        )

    return {'platforms': platforms, 'follow_platforms': follow_platforms}


def replace_unused_singulars(c):
    for m in list(c):
        msg = m.id
        if not isinstance(msg, tuple):
            continue
        if msg[0].startswith('<unused singular (hash='):
            del c[msg[0]]
            c[msg[1]] = m


def share_source_strings(catalog, shared_strings):
    """Share message IDs between catalogs to save memory.
    """
    if not shared_strings:
        shared_strings.update((m.id, m.id) for m in catalog)
        return
    for m in list(catalog):
        if not m.id:
            continue
        if m.id in shared_strings:
            m.id = shared_strings[m.id]
            catalog.delete(m.id)
            catalog[m.id] = m
        else:
            shared_strings[m.id] = m.id


def load_i18n(canonical_host, canonical_scheme, project_root, tell_sentry):
    def compute_percentage(it, total):
        return sum(
            (compute_percentage(s, len(s)) if isinstance(s, tuple) else 1) for s in it if s
        ) / total

    # Load the base locales
    localeDir = os.path.join(project_root, 'i18n', 'core')
    locales = LOCALES
    source_strings = {}
    for file in os.listdir(localeDir):
        try:
            parts = file.split(".")
            if not (len(parts) == 2 and parts[1] == "po"):
                continue
            lang = parts[0]
            with open(os.path.join(localeDir, file), 'rb') as f:
                l = Locale(lang)
                c = l.catalog = read_po(f)
                share_source_strings(c, source_strings)
                c.plural_func = get_function_from_rule(c.plural_expr)
                replace_unused_singulars(c)
                l.completion = compute_percentage(
                    (m.string for m in c if m.id and not m.fuzzy), len(c)
                )
                if l.completion == 0:
                    continue
                else:
                    locales[lang.lower()] = l
                try:
                    l.countries = make_sorted_dict(COUNTRIES, l.territories)
                except KeyError:
                    l.countries = COUNTRIES
                try:
                    l.languages_2 = make_sorted_dict(LANGUAGES_2, l.languages)
                except KeyError:
                    l.languages_2 = LANGUAGES_2
        except Exception as e:
            tell_sentry(e)
    del source_strings

    # Load the variants
    for loc_id in babel.localedata.locale_identifiers():
        if loc_id in locales:
            continue
        i = loc_id.rfind('_')
        if i == -1:
            continue
        base = locales.get(loc_id[:i])
        if base:
            l = locales[loc_id.lower()] = Locale.parse(loc_id)
            l.catalog = base.catalog
            l.completion = base.completion
            l.countries = base.countries
            l.languages_2 = base.languages_2

    # Unload the Babel data that we no longer need
    # We load a lot of data to populate the LANGUAGE_NAMES dict, we don't want
    # to keep it all in RAM.
    used_data_dict_addresses = set(id(l._data._data) for l in locales.values())
    for key, data_dict in list(babel.localedata._cache.items()):
        if id(data_dict) not in used_data_dict_addresses:
            del babel.localedata._cache[key]

    # Prepare a unique and sorted list for use in the language switcher
    loc_url = canonical_scheme+'://%s.'+canonical_host
    domain, port = (canonical_host.split(':') + [None])[:2]
    port = int(port) if port else socket.getservbyname(canonical_scheme, 'tcp')
    subdomains = {
        l.subdomain: loc_url % l.subdomain for l in locales.values()
        if not l.territory and resolve(l.subdomain + '.' + domain, port)
    }
    lang_list = sorted(
        (
            (l.completion, l.language, l.language_name.title(), loc_url % l.subdomain)
            for l in set(locales.values()) if not l.territory and l.completion > 0.5
        ),
        key=lambda t: (-t[0], t[1]),
    )

    # Add year-less date format
    year_re = re.compile(r'(^y+[^a-zA-Z]+|[^a-zA-Z]+y+$)')
    for l in locales.values():
        short_format = l.date_formats['short'].pattern
        assert short_format[0] == 'y' or short_format[-1] == 'y', (l.language, short_format)
        l.date_formats['short_yearless'] = year_re.sub('', short_format)

    # Add aliases
    for k, v in list(locales.items()):
        locales.setdefault(ALIASES.get(k, k), v)
        locales.setdefault(ALIASES_R.get(k, k), v)
    for k, v in list(locales.items()):
        locales.setdefault(k.split('_', 1)[0], v)

    # Add universal strings
    # These strings don't need to be translated, but they have to be in the catalogs
    # so that they're counted as translated.
    for l in locales.values():
        l.catalog.add("PayPal", "PayPal")

    # Patch the locales to look less formal
    locales['fr'].currency_formats['standard'] = parse_pattern('#,##0.00\u202f\xa4')
    locales['fr'].currencies['USD'] = 'dollar états-unien'

    # Load the markdown files
    docs = {}
    heading_re = re.compile(r'^(#+ )', re.M)
    for path in find_files(os.path.join(project_root, 'i18n'), '*.md'):
        d, b = os.path.split(path)
        doc = os.path.basename(d)
        lang = b[:-3]
        with open(path, 'rb') as f:
            md = f.read().decode('utf8')
            if md.startswith('# '):
                md = '\n'.join(md.split('\n')[1:]).strip()
                md = heading_re.sub(r'##\1', md)
            docs.setdefault(doc, {}).__setitem__(lang, markdown.render(md))

    return {'docs': docs, 'lang_list': lang_list, 'locales': locales, 'subdomains': subdomains}


def asset_url_generator(env, asset_url, tell_sentry, www_root):
    def asset(*paths):
        for path in paths:
            fspath = www_root+'/assets/'+path
            etag = ''
            try:
                if env.cache_static:
                    etag = asset_etag(fspath)
                else:
                    os.stat(fspath)
            except FileNotFoundError as e:
                if path == paths[-1]:
                    if not os.path.exists(fspath + '.spt'):
                        tell_sentry(e)
                else:
                    continue
            except Exception as e:
                tell_sentry(e)
            return asset_url+path+(etag and '?etag='+etag)
    return {'asset': asset}


def load_scss_variables(project_root):
    """Build a dict representing the `style/variables.scss` file.
    """
    # Get the names of all the variables
    with open(project_root + '/style/variables.scss') as f:
        variables = f.read()
    names = [m.group(1) for m in re.finditer(r'^\$([\w-]+):', variables, re.M)]
    # Compile a big rule that uses all the variables
    props = ''.join('-x-{0}: ${0};'.format(name) for name in names)
    css = sass.compile(string=('%s\nx { %s }' % (variables, props)))
    # Read the final values from the generated CSS
    d = dict((m.group(1), m.group(2)) for m in re.finditer(r'-x-([\w-]+): (.+?);\s', css))
    return {'scss_variables': d}


def s3(env):
    key, secret = env.aws_access_key_id, env.aws_secret_access_key
    if key and secret:
        s3 = boto3.client('s3', aws_access_key_id=key, aws_secret_access_key=secret)
    else:
        s3 = None
    return {'s3': s3}


def currency_exchange_rates(db):
    if not db:
        return
    return {'currency_exchange_rates': get_currency_exchange_rates(db)}


minimal_chain = StateChain(
    version,
    make_sentry_teller,
    database,
)

full_chain = StateChain(
    version,
    make_sentry_teller,
    crypto,
    database,
    canonical,
    csp,
    app_conf,
    mail,
    billing,
    stripe,
    username_restrictions,
    load_i18n,
    asset_url_generator,
    accounts_elsewhere,
    load_scss_variables,
    s3,
    currency_exchange_rates,
)


def main():
    from os import environ
    environ['RUN_CRON_JOBS'] = 'no'
    from liberapay.main import website
    app_conf, env = website.app_conf, website.env
    if app_conf.missing or app_conf.mistyped or env.missing or env.malformed:
        raise SystemExit('The configuration is incorrect.')


if __name__ == '__main__':
    main()
