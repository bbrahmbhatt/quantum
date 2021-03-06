#!/usr/bin/env python
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 OpenStack, LLC
# Copyright 2012, Big Switch Networks, Inc.
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
#
# Based on openstack generic code
# @author: Mandeep Dhami, Big Switch Networks, Inc.

"""Determine version of QuantumRestProxy plugin"""

# if vcsversion exists, use it. Else, use LOCALBRANCH:LOCALREVISION
try:
    from bigswitch.vcsversion import version_info
except ImportError:
    version_info = {'branch_nick': u'LOCALBRANCH',
                    'revision_id': u'LOCALREVISION',
                    'revno': 0}


QUANTUMRESTPROXY_VERSION = ['2012', '1', None]
YEAR, COUNT, REVISION = QUANTUMRESTPROXY_VERSION
FINAL = False   # This becomes true at Release Candidate time


def canonical_version_string():
    return '.'.join(filter(None, QUANTUMRESTPROXY_VERSION))


def version_string():
    if FINAL:
        return canonical_version_string()
    else:
        return '%s-dev' % (canonical_version_string(),)


def vcs_version_string():
    return "%s:%s" % (version_info['branch_nick'], version_info['revision_id'])


def version_string_with_vcs():
    return "%s-%s" % (canonical_version_string(), vcs_version_string())


if __name__ == "__main__":
    print version_string_with_vcs()
