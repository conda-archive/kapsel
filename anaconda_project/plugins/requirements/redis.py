# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
"""Redis-related requirements."""

from anaconda_project.plugins.requirement import EnvVarRequirement
# don't "import from" network_util or we can't monkeypatch it in tests
import anaconda_project.plugins.network_util as network_util


class RedisRequirement(EnvVarRequirement):
    """A requirement for REDIS_URL (or another specified env var) to point to a running Redis."""

    def __init__(self, registry, env_var="REDIS_URL", options=None):
        """Extend superclass to default to REDIS_URL."""
        super(RedisRequirement, self).__init__(registry=registry, env_var=env_var, options=options)

    @property
    def title(self):
        """Override superclass to supply our title."""
        return "A running Redis server, located by a redis: URL set as %s" % (self.env_var)

    def _why_not_provided(self, environ):
        url = self._get_value_of_env_var(environ)
        if url is None:
            return self._unset_message()
        split = network_util.urlparse.urlsplit(url)
        if split.scheme != 'redis':
            return "{env_var} value '{url}' does not have 'redis:' scheme.".format(env_var=self.env_var, url=url)
        port = 6379
        if split.port is not None:
            port = split.port
        if network_util.can_connect_to_socket(split.hostname, port):
            return None
        else:
            return "Cannot connect to Redis at {url}.".format(url=url, env_var=self.env_var)

    def check_status(self, environ, local_state_file):
        """Override superclass to get our status."""
        why_not_provided = self._why_not_provided(environ)

        has_been_provided = why_not_provided is None
        if has_been_provided:
            status_description = ("Using Redis server at %s" % self._get_value_of_env_var(environ))
        else:
            status_description = why_not_provided

        return self._create_status(environ,
                                   local_state_file,
                                   has_been_provided=has_been_provided,
                                   status_description=status_description,
                                   provider_class_name='RedisProvider')
