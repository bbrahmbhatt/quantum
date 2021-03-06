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
#
# @author: Somik Behera, Nicira Networks, Inc.
# @author: Brad Hall, Nicira Networks, Inc.
# @author: Aaron Rosen, Nicira Networks, Inc.


import hashlib
import logging

import webob.exc

from quantum.api.v2 import attributes
from quantum.api.v2 import base
from quantum.common import constants
from quantum.common import exceptions as q_exc
from quantum.common import rpc as q_rpc
from quantum.common import topics
from quantum.db import api as db
from quantum.db import db_base_plugin_v2
from quantum.db import dhcp_rpc_base
from quantum.db import portsecurity_db
# NOTE: quota_db cannot be removed, it is for db model
from quantum.db import quota_db
from quantum.extensions import portsecurity as psec
from quantum.extensions import providernet as pnet
from quantum.openstack.common import cfg
from quantum.openstack.common import rpc
from quantum import policy
from quantum.plugins.nicira.nicira_nvp_plugin.common import config
from quantum.plugins.nicira.nicira_nvp_plugin.common import (exceptions
                                                             as nvp_exc)
from quantum.plugins.nicira.nicira_nvp_plugin import nicira_db
from quantum.plugins.nicira.nicira_nvp_plugin import NvpApiClient
from quantum.plugins.nicira.nicira_nvp_plugin import nvplib
from quantum.plugins.nicira.nicira_nvp_plugin import nvp_cluster
from quantum.plugins.nicira.nicira_nvp_plugin.nvp_plugin_version import (
    PLUGIN_VERSION)

LOG = logging.getLogger("QuantumPlugin")


# Provider network extension - allowed network types for the NVP Plugin
class NetworkTypes:
    """ Allowed provider network types for the NVP Plugin """
    STT = 'stt'
    GRE = 'gre'
    FLAT = 'flat'
    VLAN = 'vlan'


def parse_config():
    """Parse the supplied plugin configuration.

    :param config: a ConfigParser() object encapsulating nvp.ini.
    :returns: A tuple: (clusters, plugin_config). 'clusters' is a list of
        NVPCluster objects, 'plugin_config' is a dictionary with plugin
        parameters (currently only 'max_lp_per_bridged_ls').
    """
    nvp_options = cfg.CONF.NVP
    nvp_conf = config.ClusterConfigOptions(cfg.CONF)
    cluster_names = config.register_cluster_groups(nvp_conf)
    nvp_conf.log_opt_values(LOG, logging.DEBUG)

    clusters_options = []
    for cluster_name in cluster_names:
        clusters_options.append(
            {'name': cluster_name,
             'default_tz_uuid':
             nvp_conf[cluster_name].default_tz_uuid,
             'nvp_cluster_uuid':
             nvp_conf[cluster_name].nvp_cluster_uuid,
             'nova_zone_id':
             nvp_conf[cluster_name].nova_zone_id,
             'nvp_controller_connection':
             nvp_conf[cluster_name].nvp_controller_connection, })
    LOG.debug(_("Cluster options: %s"), clusters_options)
    return nvp_options, clusters_options


class NVPRpcCallbacks(dhcp_rpc_base.DhcpRpcCallbackMixin):

    # Set RPC API version to 1.0 by default.
    RPC_API_VERSION = '1.0'

    def create_rpc_dispatcher(self):
        '''Get the rpc dispatcher for this manager.

        If a manager would like to set an rpc API version, or support more than
        one class as the target of rpc messages, override this method.
        '''
        return q_rpc.PluginRpcDispatcher([self])


class NvpPluginV2(db_base_plugin_v2.QuantumDbPluginV2,
                  portsecurity_db.PortSecurityDbMixin):
    """
    NvpPluginV2 is a Quantum plugin that provides L2 Virtual Network
    functionality using NVP.
    """

    supported_extension_aliases = ["provider", "quotas", "port-security"]
    # Default controller cluster
    default_cluster = None

    provider_network_view = "extension:provider_network:view"
    provider_network_set = "extension:provider_network:set"
    port_security_enabled_create = "create_port:port_security_enabled"
    port_security_enabled_update = "update_port:port_security_enabled"

    def __init__(self, loglevel=None):
        if loglevel:
            logging.basicConfig(level=loglevel)
            nvplib.LOG.setLevel(loglevel)
            NvpApiClient.LOG.setLevel(loglevel)

        self.nvp_opts, self.clusters_opts = parse_config()
        self.clusters = {}
        for c_opts in self.clusters_opts:
            # Password is guaranteed to be the same across all controllers
            # in the same NVP cluster.
            cluster = nvp_cluster.NVPCluster(c_opts['name'])
            for controller_connection in c_opts['nvp_controller_connection']:
                args = controller_connection.split(':')
                try:
                    args.extend([c_opts['default_tz_uuid'],
                                 c_opts['nvp_cluster_uuid'],
                                 c_opts['nova_zone_id']])
                    cluster.add_controller(*args)
                except Exception:
                    LOG.exception(_("Invalid connection parameters for "
                                    "controller %(conn)s in cluster %(name)s"),
                                  {'conn': controller_connection,
                                   'name': c_opts['name']})
                    raise nvp_exc.NvpInvalidConnection(
                        conn_params=controller_connection)

            api_providers = [(x['ip'], x['port'], True)
                             for x in cluster.controllers]
            cluster.api_client = NvpApiClient.NVPApiHelper(
                api_providers, cluster.user, cluster.password,
                request_timeout=cluster.request_timeout,
                http_timeout=cluster.http_timeout,
                retries=cluster.retries,
                redirects=cluster.redirects,
                concurrent_connections=self.nvp_opts['concurrent_connections'],
                nvp_gen_timeout=self.nvp_opts['nvp_gen_timeout'])

            if len(self.clusters) == 0:
                first_cluster = cluster
            self.clusters[c_opts['name']] = cluster

        def_cluster_name = self.nvp_opts.default_cluster_name
        if def_cluster_name and def_cluster_name in self.clusters:
            self.default_cluster = self.clusters[def_cluster_name]
        else:
            first_cluster_name = self.clusters.keys()[0]
            if not def_cluster_name:
                LOG.info(_("Default cluster name not specified. "
                           "Using first cluster:%s"), first_cluster_name)
            elif def_cluster_name not in self.clusters:
                LOG.warning(_("Default cluster name %(def_cluster_name)s. "
                              "Using first cluster:%(first_cluster_name)s"),
                            locals())
            # otherwise set 1st cluster as default
            self.default_cluster = self.clusters[first_cluster_name]

        db.configure_db()
        # Extend the fault map
        self._extend_fault_map()
        # Set up RPC interface for DHCP agent
        self.setup_rpc()

    def _extend_fault_map(self):
        """ Extends the Quantum Fault Map

        Exceptions specific to the NVP Plugin are mapped to standard
        HTTP Exceptions
        """
        base.FAULT_MAP.update({nvp_exc.NvpInvalidNovaZone:
                               webob.exc.HTTPBadRequest,
                               nvp_exc.NvpNoMorePortsException:
                               webob.exc.HTTPBadRequest})

    def _novazone_to_cluster(self, novazone_id):
        if novazone_id in self.novazone_cluster_map:
            return self.novazone_cluster_map[novazone_id]
        LOG.debug(_("Looking for nova zone: %s"), novazone_id)
        for x in self.clusters:
            LOG.debug(_("Looking for nova zone %(novazone_id)s in "
                        "cluster: %(x)s"), locals())
            if x.zone == str(novazone_id):
                self.novazone_cluster_map[x.zone] = x
                return x
        LOG.error(_("Unable to find cluster config entry for nova zone: %s"),
                  novazone_id)
        raise nvp_exc.NvpInvalidNovaZone(nova_zone=novazone_id)

    def _find_target_cluster(self, resource):
        """ Return cluster where configuration should be applied

        If the resource being configured has a paremeter expressing
        the zone id (nova_id), then select corresponding cluster,
        otherwise return default cluster.

        """
        if 'nova_id' in resource:
            return self._novazone_to_cluster(resource['nova_id'])
        else:
            return self.default_cluster

    def _check_view_auth(self, context, resource, action):
        return policy.check(context, action, resource)

    def _enforce_set_auth(self, context, resource, action):
        return policy.enforce(context, action, resource)

    def _handle_provider_create(self, context, attrs):
        # NOTE(salvatore-orlando): This method has been borrowed from
        # the OpenvSwtich plugin, altough changed to match NVP specifics.
        network_type = attrs.get(pnet.NETWORK_TYPE)
        physical_network = attrs.get(pnet.PHYSICAL_NETWORK)
        segmentation_id = attrs.get(pnet.SEGMENTATION_ID)
        network_type_set = attributes.is_attr_set(network_type)
        physical_network_set = attributes.is_attr_set(physical_network)
        segmentation_id_set = attributes.is_attr_set(segmentation_id)
        if not (network_type_set or physical_network_set or
                segmentation_id_set):
            return

        # Authorize before exposing plugin details to client
        self._enforce_set_auth(context, attrs, self.provider_network_set)
        err_msg = None
        if not network_type_set:
            err_msg = _("%s required") % pnet.NETWORK_TYPE
        elif network_type in (NetworkTypes.GRE, NetworkTypes.STT,
                              NetworkTypes.FLAT):
            if segmentation_id_set:
                err_msg = _("Segmentation ID cannot be specified with "
                            "flat network type")
        elif network_type == NetworkTypes.VLAN:
            if not segmentation_id_set:
                err_msg = _("Segmentation ID must be specified with "
                            "vlan network type")
            elif (segmentation_id_set and
                  (segmentation_id < 1 or segmentation_id > 4094)):
                err_msg = _("%s out of range (1 to 4094)") % segmentation_id
            else:
                # Verify segment is not already allocated
                binding = nicira_db.get_network_binding_by_vlanid(
                    context.session, segmentation_id)
                if binding:
                    raise q_exc.VlanIdInUse(vlan_id=segmentation_id,
                                            physical_network=physical_network)
        else:
            err_msg = _("%(net_type_param)s %(net_type_value)s not "
                        "supported") % {'net_type_param': pnet.NETWORK_TYPE,
                                        'net_type_value': network_type}
        if err_msg:
            raise q_exc.InvalidInput(error_message=err_msg)
        # TODO(salvatore-orlando): Validate tranport zone uuid
        # which should be specified in physical_network

    def _extend_network_dict_provider(self, context, network, binding=None):
        if self._check_view_auth(context, network, self.provider_network_view):
            if not binding:
                binding = nicira_db.get_network_binding(context.session,
                                                        network['id'])
            # With NVP plugin 'normal' overlay networks will have no binding
            # TODO(salvatore-orlando) make sure users can specify a distinct
            # tz_uuid as 'provider network' for STT net type
            if binding:
                network[pnet.NETWORK_TYPE] = binding.binding_type
                network[pnet.PHYSICAL_NETWORK] = binding.tz_uuid
                network[pnet.SEGMENTATION_ID] = binding.vlan_id

    def _handle_lswitch_selection(self, cluster, network,
                                  network_binding, max_ports,
                                  allow_extra_lswitches):
        lswitches = nvplib.get_lswitches(cluster, network.id)
        try:
            # TODO find main_ls too!
            return [ls for ls in lswitches
                    if (ls['_relations']['LogicalSwitchStatus']
                        ['lport_count'] < max_ports)].pop(0)
        except IndexError:
            # Too bad, no switch available
            LOG.debug(_("No switch has available ports (%d checked)"),
                      len(lswitches))
        if allow_extra_lswitches:
            main_ls = [ls for ls in lswitches if ls['uuid'] == network.id]
            tag_dict = dict((x['scope'], x['tag']) for x in main_ls[0]['tags'])
            if 'multi_lswitch' not in tag_dict:
                nvplib.update_lswitch(cluster,
                                      main_ls[0]['uuid'],
                                      main_ls[0]['display_name'],
                                      network['tenant_id'],
                                      tags=[{'tag': 'True',
                                             'scope': 'multi_lswitch'}])
            selected_lswitch = nvplib.create_lswitch(
                cluster, network.tenant_id,
                "%s-ext-%s" % (network.name, len(lswitches)),
                network_binding.binding_type,
                network_binding.tz_uuid,
                network_binding.vlan_id,
                network.id)
            return selected_lswitch
        else:
            LOG.error(_("Maximum number of logical ports reached for "
                        "logical network %s"), network.id)
            raise nvp_exc.NvpNoMorePortsException(network=network.id)

    def setup_rpc(self):
        # RPC support for dhcp
        self.topic = topics.PLUGIN
        self.conn = rpc.create_connection(new=True)
        self.dispatcher = NVPRpcCallbacks().create_rpc_dispatcher()
        self.conn.create_consumer(self.topic, self.dispatcher,
                                  fanout=False)
        # Consume from all consumers in a thread
        self.conn.consume_in_thread()

    def get_all_networks(self, tenant_id, **kwargs):
        networks = []
        for c in self.clusters:
            networks.extend(nvplib.get_all_networks(c, tenant_id, networks))
        LOG.debug(_("get_all_networks() completed for tenant "
                    "%(tenant_id)s: %(networks)s"), locals())
        return networks

    def create_network(self, context, network):
        net_data = network['network'].copy()
        # Process the provider network extension
        self._handle_provider_create(context, net_data)
        # Replace ATTR_NOT_SPECIFIED with None before sending to NVP
        for attr, value in network['network'].iteritems():
            if value is attributes.ATTR_NOT_SPECIFIED:
                net_data[attr] = None
        # FIXME(arosen) implement admin_state_up = False in NVP
        if net_data['admin_state_up'] is False:
            LOG.warning(_("Network with admin_state_up=False are not yet "
                          "supported by this plugin. Ignoring setting for "
                          "network %s"), net_data.get('name', '<unknown>'))
        tenant_id = self._get_tenant_id_for_create(context, net_data)
        target_cluster = self._find_target_cluster(net_data)
        lswitch = nvplib.create_lswitch(target_cluster,
                                        tenant_id,
                                        net_data.get('name'),
                                        net_data.get(pnet.NETWORK_TYPE),
                                        net_data.get(pnet.PHYSICAL_NETWORK),
                                        net_data.get(pnet.SEGMENTATION_ID))
        network['network']['id'] = lswitch['uuid']

        with context.session.begin(subtransactions=True):
            new_net = super(NvpPluginV2, self).create_network(context,
                                                              network)
            self._process_network_create_port_security(context,
                                                       network['network'])
            if net_data.get(pnet.NETWORK_TYPE):
                net_binding = nicira_db.add_network_binding(
                    context.session, new_net['id'],
                    net_data.get(pnet.NETWORK_TYPE),
                    net_data.get(pnet.PHYSICAL_NETWORK),
                    net_data.get(pnet.SEGMENTATION_ID))
                self._extend_network_dict_provider(context, new_net,
                                                   net_binding)
            self._extend_network_port_security_dict(context, new_net)
        return new_net

    def delete_network(self, context, id):
        super(NvpPluginV2, self).delete_network(context, id)

        # FIXME(salvatore-orlando): Failures here might lead NVP
        # and quantum state to diverge
        pairs = self._get_lswitch_cluster_pairs(id, context.tenant_id)
        for (cluster, switches) in pairs:
            nvplib.delete_networks(cluster, id, switches)

        LOG.debug(_("delete_network completed for tenant: %s"),
                  context.tenant_id)

    def _get_lswitch_cluster_pairs(self, netw_id, tenant_id):
        """Figure out the set of lswitches on each cluster that maps to this
           network id"""
        pairs = []
        for c in self.clusters.itervalues():
            lswitches = []
            try:
                results = nvplib.get_lswitches(c, netw_id)
                lswitches.extend([ls['uuid'] for ls in results])
            except q_exc.NetworkNotFound:
                continue
            pairs.append((c, lswitches))
        if len(pairs) == 0:
            raise q_exc.NetworkNotFound(net_id=netw_id)
        LOG.debug(_("Returning pairs for network: %s"), pairs)
        return pairs

    def get_network(self, context, id, fields=None):
        with context.session.begin(subtransactions=True):
            network = self._get_network(context, id)
            net_result = self._make_network_dict(network, None)
            self._extend_network_dict_provider(context, net_result)
            self._extend_network_port_security_dict(context, net_result)

        # verify the fabric status of the corresponding
        # logical switch(es) in nvp
        try:
            # FIXME(salvatore-orlando): This is not going to work unless
            # nova_id is stored in db once multiple clusters are enabled
            cluster = self._find_target_cluster(network)
            # Returns multiple lswitches if provider network.
            lswitches = nvplib.get_lswitches(cluster, id)
            for lswitch in lswitches:
                lswitch_status = (lswitch['_relations']['LogicalSwitchStatus']
                                  ['fabric_status'])
                if not lswitch_status:
                    net_result['status'] = constants.NET_STATUS_DOWN
                    break
            else:
                net_result['status'] = constants.NET_STATUS_ACTIVE
        except Exception:
            err_msg = _("Unable to get lswitches")
            LOG.exception(err_msg)
            raise nvp_exc.NvpPluginException(err_msg=err_msg)

        # Don't do field selection here otherwise we won't be able
        # to add provider networks fields
        return self._fields(net_result, fields)

    def get_networks(self, context, filters=None, fields=None):
        nvp_lswitches = []
        with context.session.begin(subtransactions=True):
            quantum_lswitches = (
                super(NvpPluginV2, self).get_networks(context, filters))
            for net in quantum_lswitches:
                self._extend_network_dict_provider(context, net)
                self._extend_network_port_security_dict(context, net)

        if context.is_admin and not filters.get("tenant_id"):
            tenant_filter = ""
        elif filters.get("tenant_id"):
            tenant_filter = ""
            for tenant in filters.get("tenant_id"):
                tenant_filter += "&tag=%s&tag_scope=os_tid" % tenant
        else:
            tenant_filter = "&tag=%s&tag_scope=os_tid" % context.tenant_id

        lswitch_filters = "uuid,display_name,fabric_status,tags"
        lswitch_url_path = (
            "/ws.v1/lswitch?fields=%s&relations=LogicalSwitchStatus%s"
            % (lswitch_filters, tenant_filter))
        try:
            for c in self.clusters.itervalues():
                res = nvplib.get_all_query_pages(
                    lswitch_url_path, c)

                nvp_lswitches.extend(res)
        except Exception:
            err_msg = _("Unable to get logical switches")
            LOG.exception(err_msg)
            raise nvp_exc.NvpPluginException(err_msg=err_msg)

        # TODO (Aaron) This can be optimized
        if filters.get("id"):
            filtered_lswitches = []
            for nvp_lswitch in nvp_lswitches:
                for id in filters.get("id"):
                    if id == nvp_lswitch['uuid']:
                        filtered_lswitches.append(nvp_lswitch)
            nvp_lswitches = filtered_lswitches

        for quantum_lswitch in quantum_lswitches:
            for nvp_lswitch in nvp_lswitches:
                # TODO(salvatore-orlando): watch out for "extended" lswitches
                if nvp_lswitch['uuid'] == quantum_lswitch["id"]:
                    if (nvp_lswitch["_relations"]["LogicalSwitchStatus"]
                            ["fabric_status"]):
                        quantum_lswitch["status"] = constants.NET_STATUS_ACTIVE
                    else:
                        quantum_lswitch["status"] = constants.NET_STATUS_DOWN
                    quantum_lswitch["name"] = nvp_lswitch["display_name"]
                    nvp_lswitches.remove(nvp_lswitch)
                    break
            else:
                raise nvp_exc.NvpOutOfSyncException()
        # do not make the case in which switches are found in NVP
        # but not in Quantum catastrophic.
        if len(nvp_lswitches):
            LOG.warning(_("Found %s logical switches not bound "
                        "to Quantum networks. Quantum and NVP are "
                        "potentially out of sync"), len(nvp_lswitches))

        LOG.debug(_("get_networks() completed for tenant %s"),
                  context.tenant_id)

        if fields:
            ret_fields = []
            for quantum_lswitch in quantum_lswitches:
                row = {}
                for field in fields:
                    row[field] = quantum_lswitch[field]
                ret_fields.append(row)
            return ret_fields

        return quantum_lswitches

    def update_network(self, context, id, network):
        if network["network"].get("admin_state_up"):
            if network['network']["admin_state_up"] is False:
                raise q_exc.NotImplementedError(_("admin_state_up=False "
                                                  "networks are not "
                                                  "supported."))
        with context.session.begin(subtransactions=True):
            quantum_db = super(NvpPluginV2, self).update_network(
                context, id, network)
            if psec.PORTSECURITY in network['network']:
                self._update_network_security_binding(
                    context, id, network['network'][psec.PORTSECURITY])
            self._extend_network_port_security_dict(
                context, quantum_db)
        return quantum_db

    def get_ports(self, context, filters=None, fields=None):
        with context.session.begin(subtransactions=True):
            quantum_lports = super(NvpPluginV2, self).get_ports(
                context, filters)
            for quantum_lport in quantum_lports:
                self._extend_port_port_security_dict(context, quantum_lport)

        vm_filter = ""
        tenant_filter = ""
        # This is used when calling delete_network. Quantum checks to see if
        # the network has any ports.
        if filters.get("network_id"):
            # FIXME (Aaron) If we get more than one network_id this won't work
            lswitch = filters["network_id"][0]
        else:
            lswitch = "*"

        if filters.get("device_id"):
            for vm_id in filters.get("device_id"):
                vm_filter = ("%stag_scope=vm_id&tag=%s&" % (vm_filter,
                             hashlib.sha1(vm_id).hexdigest()))
        else:
            vm_id = ""

        if filters.get("tenant_id"):
            for tenant in filters.get("tenant_id"):
                tenant_filter = ("%stag_scope=os_tid&tag=%s&" %
                                 (tenant_filter, tenant))

        nvp_lports = {}

        lport_fields_str = ("tags,admin_status_enabled,display_name,"
                            "fabric_status_up")
        try:
            for c in self.clusters.itervalues():
                lport_query_path = (
                    "/ws.v1/lswitch/%s/lport?fields=%s&%s%stag_scope=q_port_id"
                    "&relations=LogicalPortStatus" %
                    (lswitch, lport_fields_str, vm_filter, tenant_filter))

                ports = nvplib.get_all_query_pages(lport_query_path, c)
                if ports:
                    for port in ports:
                        for tag in port["tags"]:
                            if tag["scope"] == "q_port_id":
                                nvp_lports[tag["tag"]] = port

        except Exception:
            err_msg = _("Unable to get ports")
            LOG.exception(err_msg)
            raise nvp_exc.NvpPluginException(err_msg=err_msg)

        lports = []
        for quantum_lport in quantum_lports:
            try:
                quantum_lport["admin_state_up"] = (
                    nvp_lports[quantum_lport["id"]]["admin_status_enabled"])

                quantum_lport["name"] = (
                    nvp_lports[quantum_lport["id"]]["display_name"])

                if (nvp_lports[quantum_lport["id"]]
                        ["_relations"]
                        ["LogicalPortStatus"]
                        ["fabric_status_up"]):
                    quantum_lport["status"] = constants.PORT_STATUS_ACTIVE
                else:
                    quantum_lport["status"] = constants.PORT_STATUS_DOWN

                del nvp_lports[quantum_lport["id"]]
                lports.append(quantum_lport)
            except KeyError:

                LOG.debug(_("Quantum logical port %s was not found on NVP"),
                          quantum_lport['id'])

        # do not make the case in which ports are found in NVP
        # but not in Quantum catastrophic.
        if len(nvp_lports):
            LOG.warning(_("Found %s logical ports not bound "
                          "to Quantum ports. Quantum and NVP are "
                          "potentially out of sync"), len(nvp_lports))

        if fields:
            ret_fields = []
            for lport in lports:
                row = {}
                for field in fields:
                    row[field] = lport[field]
                ret_fields.append(row)
            return ret_fields
        return lports

    def create_port(self, context, port):
        # If PORTSECURITY is not the default value ATTR_NOT_SPECIFIED
        # then we pass the port to the policy engine. The reason why we don't
        # pass the value to the policy engine when the port is
        # ATTR_NOT_SPECIFIED is for the case where a port is created on a
        # shared network that is not owned by the tenant.
        # TODO(arosen) fix policy engine to do this for us automatically.
        if attributes.is_attr_set(port['port'].get(psec.PORTSECURITY)):
            self._enforce_set_auth(context, port,
                                   self.port_security_enabled_create)
        port_data = port['port']
        with context.session.begin(subtransactions=True):
            # First we allocate port in quantum database
            quantum_db = super(NvpPluginV2, self).create_port(context, port)
            # Update fields obtained from quantum db (eg: MAC address)
            port["port"].update(quantum_db)

            # port security extension checks
            (port_security, has_ip) = self._determine_port_security_and_has_ip(
                context, port_data)
            port_data[psec.PORTSECURITY] = port_security
            self._process_port_security_create(context, port_data)
            # provider networking extension checks
            # Fetch the network and network binding from Quantum db
            network = self._get_network(context, port_data['network_id'])
            network_binding = nicira_db.get_network_binding(
                context.session, port_data['network_id'])
            max_ports = self.nvp_opts.max_lp_per_overlay_ls
            allow_extra_lswitches = False
            if (network_binding and
                network_binding.binding_type in (NetworkTypes.FLAT,
                                                 NetworkTypes.VLAN)):
                max_ports = self.nvp_opts.max_lp_per_bridged_ls
                allow_extra_lswitches = True
            try:
                q_net_id = port_data['network_id']
                cluster = self._find_target_cluster(port_data)
                selected_lswitch = self._handle_lswitch_selection(
                    cluster, network, network_binding, max_ports,
                    allow_extra_lswitches)
                lswitch_uuid = selected_lswitch['uuid']
                lport = nvplib.create_lport(cluster,
                                            lswitch_uuid,
                                            port_data['tenant_id'],
                                            port_data['id'],
                                            port_data['name'],
                                            port_data['device_id'],
                                            port_data['admin_state_up'],
                                            port_data['mac_address'],
                                            port_data['fixed_ips'],
                                            port_data[psec.PORTSECURITY])
                # Get NVP ls uuid for quantum network
                nvplib.plug_interface(cluster, selected_lswitch['uuid'],
                                      lport['uuid'], "VifAttachment",
                                      port_data['id'])
            except nvp_exc.NvpNoMorePortsException as e:
                LOG.error(_("Number of available ports for network %s "
                            "exhausted"), port_data['network_id'])
                raise e
            except Exception:
                # failed to create port in NVP delete port from quantum_db
                # FIXME (arosen) or the plugin_interface call failed in which
                # case we need to garbage collect the left over port in nvp.
                err_msg = _("An exception occured while plugging the interface"
                            " in NVP for port %s") % port_data['id']
                LOG.exception(err_msg)
                raise nvp_exc.NvpPluginException(err_desc=err_msg)

            LOG.debug(_("create_port completed on NVP for tenant "
                        "%(tenant_id)s: (%(id)s)"), port_data)

            self._extend_port_port_security_dict(context, port_data)
        return port_data

    def update_port(self, context, id, port):
        self._enforce_set_auth(context, port,
                               self.port_security_enabled_update)
        tenant_id = self._get_tenant_id_for_create(context, port)
        with context.session.begin(subtransactions=True):
            ret_port = super(NvpPluginV2, self).update_port(
                context, id, port)
            # copy values over
            ret_port.update(port['port'])

            # Handle port security
            if psec.PORTSECURITY in port['port']:
                self._update_port_security_binding(
                    context, id, ret_port[psec.PORTSECURITY])
            # populate with value
            else:
                ret_port[psec.PORTSECURITY] = self._get_port_security_binding(
                    context, id)

            port_nvp, cluster = (
                nvplib.get_port_by_quantum_tag(self.clusters.itervalues(),
                                               ret_port["network_id"], id))
            LOG.debug(_("Update port request: %s"), port)
            nvplib.update_port(cluster, ret_port['network_id'],
                               port_nvp['uuid'], id, tenant_id,
                               ret_port['name'], ret_port['device_id'],
                               ret_port['admin_state_up'],
                               ret_port['mac_address'],
                               ret_port['fixed_ips'],
                               ret_port[psec.PORTSECURITY])

        # Update the port status from nvp. If we fail here hide it since
        # the port was successfully updated but we were not able to retrieve
        # the status.
        try:
            ret_port['status'] = nvplib.get_port_status(
                cluster, ret_port['network_id'], port_nvp['uuid'])
        except:
            LOG.warn(_("Unable to retrieve port status for: %s."),
                     port_nvp['uuid'])
        return ret_port

    def delete_port(self, context, id):
        # TODO(salvatore-orlando): pass only actual cluster
        port, cluster = nvplib.get_port_by_quantum_tag(
            self.clusters.itervalues(), '*', id)
        if port is None:
            raise q_exc.PortNotFound(port_id=id)
        # TODO(bgh): if this is a bridged network and the lswitch we just got
        # back will have zero ports after the delete we should garbage collect
        # the lswitch.
        nvplib.delete_port(cluster, port)

        LOG.debug(_("delete_port() completed for tenant: %s"),
                  context.tenant_id)
        return super(NvpPluginV2, self).delete_port(context, id)

    def get_port(self, context, id, fields=None):
        quantum_db = super(NvpPluginV2, self).get_port(context, id, fields)

        #TODO: pass only the appropriate cluster here
        #Look for port in all lswitches
        port, cluster = (
            nvplib.get_port_by_quantum_tag(self.clusters.itervalues(),
                                           "*", id))

        quantum_db["admin_state_up"] = port["admin_status_enabled"]
        if port["_relations"]["LogicalPortStatus"]["fabric_status_up"]:
            quantum_db["status"] = constants.PORT_STATUS_ACTIVE
        else:
            quantum_db["status"] = constants.PORT_STATUS_DOWN

        LOG.debug(_("Port details for tenant %(tenant_id)s: %(quantum_db)s"),
                  {'tenant_id': context.tenant_id, 'quantum_db': quantum_db})
        return quantum_db

    def get_plugin_version(self):
        return PLUGIN_VERSION
