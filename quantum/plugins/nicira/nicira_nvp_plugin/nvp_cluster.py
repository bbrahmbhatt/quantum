# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 Nicira, Inc.
# All Rights Reserved
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


class NVPCluster(object):
    """Encapsulates controller connection and api_client for a cluster.

    Accessed within the NvpPluginV2 class.

    Each element in the self.controllers list is a dictionary that
    contains the following keys:
        ip, port, user, password, default_tz_uuid, uuid, zone

    There may be some redundancy here, but that has been done to provide
    future flexibility.
    """

    def __init__(self, name):
        self._name = name
        self.controllers = []
        self.api_client = None

    def __repr__(self):
        ss = ['{ "NVPCluster": [']
        ss.append('{ "name" : "%s" }' % self.name)
        ss.append(',')
        for c in self.controllers:
            ss.append(str(c))
            ss.append(',')
        ss.append('] }')
        return ''.join(ss)

    def add_controller(self, ip, port, user, password, request_timeout,
                       http_timeout, retries, redirects,
                       default_tz_uuid, uuid=None, zone=None):
        """Add a new set of controller parameters.

        :param ip: IP address of controller.
        :param port: port controller is listening on.
        :param user: user name.
        :param password: user password.
        :param request_timeout: timeout for an entire API request.
        :param http_timeout: timeout for a connect to a controller.
        :param retries: maximum number of request retries.
        :param redirects: maximum number of server redirect responses to
            follow.
        :param default_tz_uuid: default transport zone uuid.
        :param uuid: UUID of this cluster (used in MDI configs).
        :param zone: Zone of this cluster (used in MDI configs).
        """

        keys = [
            'ip', 'user', 'password', 'default_tz_uuid', 'uuid', 'zone']
        controller_dict = dict([(k, locals()[k]) for k in keys])

        int_keys = [
            'port', 'request_timeout', 'http_timeout', 'retries', 'redirects']
        for k in int_keys:
            controller_dict[k] = int(locals()[k])

        self.controllers.append(controller_dict)

    def get_controller(self, idx):
        return self.controllers[idx]

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, val=None):
        self._name = val

    @property
    def host(self):
        return self.controllers[0]['ip']

    @property
    def port(self):
        return self.controllers[0]['port']

    @property
    def user(self):
        return self.controllers[0]['user']

    @property
    def password(self):
        return self.controllers[0]['password']

    @property
    def request_timeout(self):
        return self.controllers[0]['request_timeout']

    @property
    def http_timeout(self):
        return self.controllers[0]['http_timeout']

    @property
    def retries(self):
        return self.controllers[0]['retries']

    @property
    def redirects(self):
        return self.controllers[0]['redirects']

    @property
    def default_tz_uuid(self):
        return self.controllers[0]['default_tz_uuid']

    @property
    def zone(self):
        return self.controllers[0]['zone']

    @property
    def uuid(self):
        return self.controllers[0]['uuid']
