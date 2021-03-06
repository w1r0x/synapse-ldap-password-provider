# -*- coding: utf-8 -*-
# Copyright 2017 Slipeer <slipeer@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer, threads
import logging
import time

__version__ = '1'
logger = logging.getLogger('ldap')

try:
    import ldap3
    import ldap3.core.exceptions

    try:
        LDAP_AUTH_SIMPLE = ldap3.AUTH_SIMPLE
    except AttributeError:
        LDAP_AUTH_SIMPLE = ldap3.SIMPLE

    try:
        ldap3.set_config_parameter('DEFAULT_ENCODING', 'UTF-8')
    except AttributeError:
        pass
    except ldap3.core.exceptions.LDAPConfigurationParameterError:
        pass

except ImportError:
    ldap3 = None
    pass


class LDAPPasswordProvider(object):
    __version__ = '1'

    def __init__(self, config, account_handler):
        self.account_handler = account_handler

        if not ldap3:
            raise RuntimeError(
                'Missing ldap3 library. '
                'This is required for LDAP Authentication.'
            )

        self.ldap_mode = config.mode
        self.ldap_uri = config.uri
        self.ldap_start_tls = config.start_tls
        self.ldap_base = config.base
        self.ldap_attributes = config.attributes
        if self.ldap_mode == 'search':
            self.ldap_bind_dn = config.bind_dn
            self.ldap_bind_password = config.bind_password
            self.ldap_filter = config.filter

        # If you do not want your internal users to be blocked from outside
        # by scrambling passwords through this service, then you need
        # implement a more rigid account lockout policy then in yor LDAP server
        self.ldap_alp_exists = config.account_lockout_policy_exists
        if self.ldap_alp_exists:
            self.ldap_alp = config.account_lockout_policy
        self.bad_login_attemps = {}

    @defer.inlineCallbacks
    def check_password(self, user_id, password):
        """ Authenticate a user against an LDAP Server
            and register an account if none exists.

            Returns:
                True if authentication against LDAP was successful
        """
        if not password:
            defer.returnValue(False)
        user_id = user_id.lower()
        localpart = user_id.split(":", 1)[0][1:]

        now = time.time()
        if localpart in self.bad_login_attemps.keys():
            if self.bad_login_attemps[localpart]['count'] >= self.ldap_alp['attemps']:
                unlock_time = self.bad_login_attemps[localpart]['ts'] + \
                    self.ldap_alp['locktime_s']
                if now <= unlock_time:
                    logger.error(
                        'User %s is locked by account lockout policy. '
                        'This login attemp will fail. '
                        'Seconds to unlock: %d' %
                        (user_id, unlock_time - now)
                    )
                    defer.returnValue(False)

        try:
            server = ldap3.Server(self.ldap_uri, get_info=None)
            logger.debug(
                'LDAP connection with %s',
                self.ldap_uri
            )

            if self.ldap_mode == 'simple':
                bind_dn = "{prop}={value},{base}".format(
                    prop=self.ldap_attributes['uid'],
                    value=localpart,
                    base=self.ldap_base
                )
                result, conn = yield self._ldap_simple_bind(
                    server=server, bind_dn=bind_dn, password=password
                )
                logger.debug(
                    'LDAP authentication method simple bind returned: '
                    '%s (conn: %s)',
                    result,
                    conn
                )
                if not result:
                    if self.ldap_alp_exists:
                        if localpart in self.bad_login_attemps.keys():
                            self.bad_login_attemps[localpart]['count'] += 1
                            self.bad_login_attemps[localpart]['ts'] = now
                        else:
                            self.bad_login_attemps[localpart] = {
                                'count': 1,
                                'ts': now
                            }
                    defer.returnValue(False)
            elif self.ldap_mode == 'search':
                result, conn = yield self._ldap_authenticated_search(
                    server=server, localpart=localpart, password=password
                )
                logger.debug(
                    'LDAP auth method authenticated search returned: '
                    '%s (conn: %s)',
                    result,
                    conn
                )
                if not result:
                    if self.ldap_alp_exists:
                        if localpart in self.bad_login_attemps.keys():
                            self.bad_login_attemps[localpart]['count'] += 1
                            self.bad_login_attemps[localpart]['ts'] = now
                        else:
                            self.bad_login_attemps[localpart] = {
                                'count': 1,
                                'ts': now
                            }
                    defer.returnValue(False)
            else:
                raise RuntimeError(
                    'Invalid LDAP mode specified: {%s}' %
                    self.ldap_mode
                )
            # ???
            try:
                logger.info(
                    'User authenticated against LDAP server: %s',
                    conn
                )
            except NameError:
                logger.warning(
                    'Authentication method yielded no LDAP connection, '
                    'aborting!'
                )
                defer.returnValue(False)

            query = '({prop}={value})'.format(
                prop=self.ldap_attributes['uid'],
                value=localpart
            )

            if self.ldap_mode == 'search' and self.ldap_filter:
                query = '(&{filter}{user_filter})'.format(
                    filter=query,
                    user_filter=self.ldap_filter
                )
            logger.debug(
                'LDAP search filter: %s',
                query
            )

            yield threads.deferToThread(
                conn.search,
                search_base=self.ldap_base,
                search_filter=query,
                attributes=self.ldap_attributes.values()
            )

            responses = [
                response
                for response
                in conn.response
                if response['type'] == 'searchResEntry'
            ]

            if len(responses) == 1:
                attrs = responses[0]['attributes']
                try:
                    name = attrs[self.ldap_attributes['name']][0]
                except:
                    name = None

                store = self.account_handler.hs.get_handlers().profile_handler.store
                if not (yield self.account_handler.check_user_exists(user_id)):
                    # Create account if not exists
                    user_id, access_token = (
                        yield self.account_handler.register(localpart=localpart)
                    )

                if name is not None:
                    # Update user Display Name
                    yield store.set_profile_displayname(localpart, name)

                if 'mail' in self.ldap_attributes:
                    for mail in attrs[self.ldap_attributes['mail']]:
                        # Update user email
                        validated_at = self.account_handler.hs.get_clock().time_msec()
                        user_id_by_threepid = yield store.get_user_id_by_threepid(
                            'email',
                            mail.lower()
                        )
                        # add email only if not exists
                        if not user_id_by_threepid:
                            yield store.user_add_threepid(
                                user_id,
                                'email',
                                mail,
                                validated_at,
                                validated_at
                            )
                        elif not user_id_by_threepid.lower() == user_id.lower():
                            logger.error(
                                'Auth user %s with %s email but user %s'
                                'already have same email' % (
                                    user_id,
                                    mail.lower(),
                                    user_id_by_threepid
                                )
                            )

                if 'msisdn' in self.ldap_attributes:
                    for msisdn in attrs[self.ldap_attributes['msisdn']]:
                        # Update user msisdn
                        validated_at = self.account_handler.hs.get_clock().time_msec()
                        user_id_by_threepid = yield store.get_user_id_by_threepid(
                            'msisdn',
                            msisdn
                        )
                        # add msisdn only if not exists
                        if not user_id_by_threepid:
                            yield store.user_add_threepid(
                                user_id,
                                'msisdn',
                                msisdn,
                                validated_at,
                                validated_at
                            )
                        elif not user_id_by_threepid.lower() == user_id.lower():
                            logger.error(
                                'Auth user %s with %s msisdn but user %s'
                                'already have same msisdn' % (
                                    user_id,
                                    msisdn,
                                    user_id_by_threepid
                                )
                            )

                logger.info(
                    'Auth based on LDAP data was successful: '
                    '%s: %s (%s, %s)',
                    user_id, localpart, mail
                )
                if localpart in self.bad_login_attemps:
                    del self.bad_login_attemps[localpart]
                defer.returnValue(True)
            else:
                if len(responses) == 0:
                    logger.warning('LDAP auth failed, no result.')
                else:
                    logger.warning(
                        'LDAP auth failed, too many results (%s)',
                        len(responses)
                    )
                defer.returnValue(False)

            defer.returnValue(False)

        except ldap3.core.exceptions.LDAPException as e:
            logger.warning('Error during ldap authentication: %s', e)
            defer.returnValue(False)

    @staticmethod
    def parse_config(config):
        class _LdapConfig(object):
            pass

        def _require_keys(config, required):
            missing = [key for key in required if key not in config]
            if missing:
                raise Exception(
                    'LDAP enabled but missing required config values: %s' %
                    ', '.join(missing)
                )

        ldap_config = _LdapConfig()
        ldap_config.enabled = config.get('enabled', False)
        ldap_config.mode = 'simple'

        # verify config sanity
        _require_keys(config, [
            'uri',
            'base',
            'attributes',
        ])

        ldap_config.uri = config['uri']
        ldap_config.start_tls = config.get('start_tls', False)
        ldap_config.base = config['base']
        ldap_config.attributes = config['attributes']

        if 'bind_dn' in config:
            ldap_config.mode = 'search'
            _require_keys(config, [
                'bind_dn',
                'bind_password',
            ])

            ldap_config.bind_dn = config['bind_dn']
            ldap_config.bind_password = config['bind_password']
            ldap_config.filter = config.get('filter', None)

        # verify attribute lookup
        _require_keys(config['attributes'], [
            'uid',
            'name',
        ])

        if 'account_lockout_policy' in config:
            ldap_config.account_lockout_policy_exists = True
            ldap_config.account_lockout_policy = config['account_lockout_policy']
            _require_keys(config['account_lockout_policy'], [
                'attemps',
                'locktime_s',
            ])
        else:
            ldap_config.account_lockout_policy_exists = False

        return ldap_config

    @defer.inlineCallbacks
    def _ldap_simple_bind(self, server, bind_dn, password):
        """ Attempt a simple bind with the credentials
            given by the user against the LDAP server.

            Returns True, LDAP3Connection
                if the bind was successful
            Returns False, None
                if an error occured
        """

        try:
            # bind with the the local users ldap credentials
            conn = yield threads.deferToThread(
                ldap3.Connection,
                server, bind_dn, password,
                authentication=LDAP_AUTH_SIMPLE,
                read_only=True,
            )
            logger.debug(
                'LDAP connection in simple bind mode: %s',
                conn
            )

            if self.ldap_start_tls:
                yield threads.deferToThread(conn.open)
                yield threads.deferToThread(conn.start_tls)
                logger.debug(
                    'Upgraded LDAP connection in simple bind mode through '
                    'StartTLS: %s',
                    conn
                )

            if (yield threads.deferToThread(conn.bind)):
                logger.debug('LDAP Bind successful in simple bind mode.')
                defer.returnValue((True, conn))

            logger.info(
                'LDAP bind failed for %s failed: %s',
                bind_dn, conn.result['description']
            )
            yield threads.deferToThread(conn.unbind)
            defer.returnValue((False, None))

        except ldap3.core.exceptions.LDAPException as e:
            logger.warning('LDAP authentication error: %s', e)
            defer.returnValue((False, None))

    @defer.inlineCallbacks
    def _ldap_authenticated_search(self, server, localpart, password):
        """ Attempt to login with the preconfigured bind_dn
            and then continue searching and filtering within
            the base_dn

            Returns (True, LDAP3Connection)
                if a single matching DN within the base was found
                that matched the filter expression, and with which
                a successful bind was achieved

                The LDAP3Connection returned is the instance that was used to
                verify the password not the one using the configured bind_dn.
            Returns (False, None)
                if an error occured
        """

        try:
            conn = yield threads.deferToThread(
                ldap3.Connection,
                server,
                self.ldap_bind_dn,
                self.ldap_bind_password
            )
            logger.debug(
                'LDAP connection in search mode: %s',
                conn
            )

            if self.ldap_start_tls:
                yield threads.deferToThread(conn.open)
                yield threads.deferToThread(conn.start_tls)
                logger.debug(
                    'Upgraded LDAP connection in search mode through '
                    'StartTLS: %s',
                    conn
                )

            if not (yield threads.deferToThread(conn.bind)):
                logger.warning(
                    'LDAP bind with `bind_dn` failed: %s',
                    conn.result['description']
                )
                yield threads.deferToThread(conn.unbind)
                defer.returnValue((False, None))

            # construct search_filter like (uid=localpart)
            query = '({prop}={value})'.format(
                prop=self.ldap_attributes['uid'],
                value=localpart
            )
            if self.ldap_filter:
                # combine with the AND expression
                query = '(&{query}{filter})'.format(
                    query=query,
                    filter=self.ldap_filter
                )
            logger.debug(
                'LDAP search filter: %s',
                query
            )
            yield threads.deferToThread(
                conn.search,
                search_base=self.ldap_base,
                search_filter=query
            )

            responses = [
                response
                for response
                in conn.response
                if response['type'] == 'searchResEntry'
            ]

            if len(responses) == 1:
                user_dn = responses[0]['dn']
                logger.debug('LDAP search found dn: %s', user_dn)

                yield threads.deferToThread(conn.unbind)
                result = yield self._ldap_simple_bind(
                    server=server, bind_dn=user_dn, password=password
                )

                defer.returnValue(result)
            else:
                if len(responses) == 0:
                    logger.info(
                        'LDAP search returned no results for %s',
                        localpart
                    )
                else:
                    logger.info(
                        'LDAP search returned too many (%s) results for %s',
                        len(responses), localpart
                    )
                yield threads.deferToThread(conn.unbind)
                defer.returnValue((False, None))

        except ldap3.core.exceptions.LDAPException as e:
            logger.warning('LDAP authentication error: %s', e)
            defer.returnValue((False, None))
