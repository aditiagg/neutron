# Copyright (c) 2012 OpenStack Foundation.
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

import contextlib
import sys
import time

import mock
import netaddr
from oslo_config import cfg
from oslo_log import log
import testtools

from neutron.agent.linux import async_process
from neutron.agent.linux import ip_lib
from neutron.agent.linux import ovs_lib
from neutron.agent.linux import utils
from neutron.common import constants as n_const
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2.drivers.l2pop import rpc as l2pop_rpc
from neutron.plugins.openvswitch.agent import ovs_neutron_agent
from neutron.plugins.openvswitch.common import constants
from neutron.tests import base


NOTIFIER = 'neutron.plugins.ml2.rpc.AgentNotifierApi'
OVS_LINUX_KERN_VERS_WITHOUT_VXLAN = "3.12.0"

FAKE_MAC = '00:11:22:33:44:55'
FAKE_IP1 = '10.0.0.1'
FAKE_IP2 = '10.0.0.2'


class CreateAgentConfigMap(base.BaseTestCase):

    def test_create_agent_config_map_succeeds(self):
        self.assertTrue(ovs_neutron_agent.create_agent_config_map(cfg.CONF))

    def test_create_agent_config_map_fails_for_invalid_tunnel_config(self):
        # An ip address is required for tunneling but there is no default,
        # verify this for both gre and vxlan tunnels.
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            ovs_neutron_agent.create_agent_config_map(cfg.CONF)
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_VXLAN],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            ovs_neutron_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_fails_no_local_ip(self):
        # An ip address is required for tunneling but there is no default
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_VXLAN],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            ovs_neutron_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_fails_for_invalid_tunnel_type(self):
        cfg.CONF.set_override('tunnel_types', ['foobar'], group='AGENT')
        with testtools.ExpectedException(ValueError):
            ovs_neutron_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_multiple_tunnel_types(self):
        cfg.CONF.set_override('local_ip', '10.10.10.10', group='OVS')
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE,
                              p_const.TYPE_VXLAN], group='AGENT')
        cfgmap = ovs_neutron_agent.create_agent_config_map(cfg.CONF)
        self.assertEqual(cfgmap['tunnel_types'],
                         [p_const.TYPE_GRE, p_const.TYPE_VXLAN])

    def test_create_agent_config_map_enable_distributed_routing(self):
        self.addCleanup(cfg.CONF.reset)
        # Verify setting only enable_tunneling will default tunnel_type to GRE
        cfg.CONF.set_override('enable_distributed_routing', True,
                              group='AGENT')
        cfgmap = ovs_neutron_agent.create_agent_config_map(cfg.CONF)
        self.assertEqual(cfgmap['enable_distributed_routing'], True)


class TestOvsNeutronAgent(base.BaseTestCase):

    def setUp(self):
        super(TestOvsNeutronAgent, self).setUp()
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        cfg.CONF.set_default('firewall_driver',
                             'neutron.agent.firewall.NoopFirewallDriver',
                             group='SECURITYGROUP')
        cfg.CONF.set_default('quitting_rpc_timeout', 10, 'AGENT')
        kwargs = ovs_neutron_agent.create_agent_config_map(cfg.CONF)

        class MockFixedIntervalLoopingCall(object):
            def __init__(self, f):
                self.f = f

            def start(self, interval=0):
                self.f()

        with contextlib.nested(
            mock.patch('neutron.plugins.openvswitch.agent.ovs_neutron_agent.'
                       'OVSNeutronAgent.setup_integration_br'),
            mock.patch('neutron.plugins.openvswitch.agent.ovs_neutron_agent.'
                       'OVSNeutronAgent.setup_ancillary_bridges',
                       return_value=[]),
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'create'),
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'set_secure_mode'),
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'get_local_port_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.linux.ovs_lib.BaseOVS.get_bridges'),
            mock.patch('neutron.openstack.common.loopingcall.'
                       'FixedIntervalLoopingCall',
                       new=MockFixedIntervalLoopingCall)):
            self.agent = ovs_neutron_agent.OVSNeutronAgent(**kwargs)
            # set back to true because initial report state will succeed due
            # to mocked out RPC calls
            self.agent.use_call = True
            self.agent.tun_br = mock.Mock()
        self.agent.sg_agent = mock.Mock()

    def _mock_port_bound(self, ofport=None, new_local_vlan=None,
                         old_local_vlan=None):
        port = mock.Mock()
        port.ofport = ofport
        net_uuid = 'my-net-uuid'
        fixed_ips = [{'subnet_id': 'my-subnet-uuid',
                      'ip_address': '1.1.1.1'}]
        if old_local_vlan is not None:
            self.agent.local_vlan_map[net_uuid] = (
                ovs_neutron_agent.LocalVLANMapping(
                    old_local_vlan, None, None, None))
        with contextlib.nested(
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'set_db_attribute', return_value=True),
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'db_get_val', return_value=old_local_vlan),
            mock.patch.object(self.agent.int_br, 'delete_flows')
        ) as (set_ovs_db_func, get_ovs_db_func, delete_flows_func):
            self.agent.port_bound(port, net_uuid, 'local', None, None,
                                  fixed_ips, "compute:None", False)
        get_ovs_db_func.assert_called_once_with("Port", mock.ANY, "tag")
        if new_local_vlan != old_local_vlan:
            set_ovs_db_func.assert_called_once_with(
                "Port", mock.ANY, "tag", new_local_vlan)
            if ofport != -1:
                delete_flows_func.assert_called_once_with(in_port=port.ofport)
            else:
                self.assertFalse(delete_flows_func.called)
        else:
            self.assertFalse(set_ovs_db_func.called)
            self.assertFalse(delete_flows_func.called)

    def test_check_agent_configurations_raises(self):
        self.agent.enable_distributed_routing = True
        self.agent.l2_pop = False
        self.assertRaises(ValueError,
                          self.agent._check_agent_configurations)

    def test_check_agent_configurations(self):
        self.agent.enable_distributed_routing = True
        self.agent.l2_pop = True
        self.assertIsNone(self.agent._check_agent_configurations())

    def test_port_bound_deletes_flows_for_valid_ofport(self):
        self._mock_port_bound(ofport=1, new_local_vlan=1)

    def test_port_bound_ignores_flows_for_invalid_ofport(self):
        self._mock_port_bound(ofport=-1, new_local_vlan=1)

    def test_port_bound_does_not_rewire_if_already_bound(self):
        self._mock_port_bound(ofport=-1, new_local_vlan=1, old_local_vlan=1)

    def _test_port_dead(self, cur_tag=None):
        port = mock.Mock()
        port.ofport = 1
        with contextlib.nested(
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'set_db_attribute', return_value=True),
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'db_get_val', return_value=cur_tag),
            mock.patch.object(self.agent.int_br, 'add_flow')
        ) as (set_ovs_db_func, get_ovs_db_func, add_flow_func):
            self.agent.port_dead(port)
        get_ovs_db_func.assert_called_once_with("Port", mock.ANY, "tag")
        if cur_tag == ovs_neutron_agent.DEAD_VLAN_TAG:
            self.assertFalse(set_ovs_db_func.called)
            self.assertFalse(add_flow_func.called)
        else:
            set_ovs_db_func.assert_called_once_with(
                "Port", mock.ANY, "tag", ovs_neutron_agent.DEAD_VLAN_TAG)
            add_flow_func.assert_called_once_with(
                priority=2, in_port=port.ofport, actions="drop")

    def test_port_dead(self):
        self._test_port_dead()

    def test_port_dead_with_port_already_dead(self):
        self._test_port_dead(ovs_neutron_agent.DEAD_VLAN_TAG)

    def mock_scan_ports(self, vif_port_set=None, registered_ports=None,
                        updated_ports=None, port_tags_dict=None):
        if port_tags_dict is None:  # Because empty dicts evaluate as False.
            port_tags_dict = {}
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'get_vif_port_set',
                              return_value=vif_port_set),
            mock.patch.object(self.agent.int_br, 'get_port_tag_dict',
                              return_value=port_tags_dict)
        ):
            return self.agent.scan_ports(registered_ports, updated_ports)

    def test_scan_ports_returns_current_only_for_unchanged_ports(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 3])
        expected = {'current': vif_port_set}
        actual = self.mock_scan_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_returns_port_changes(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        expected = dict(current=vif_port_set, added=set([3]), removed=set([2]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def _test_scan_ports_with_updated_ports(self, updated_ports):
        vif_port_set = set([1, 3, 4])
        registered_ports = set([1, 2, 4])
        expected = dict(current=vif_port_set, added=set([3]),
                        removed=set([2]), updated=set([4]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_finds_known_updated_ports(self):
        self._test_scan_ports_with_updated_ports(set([4]))

    def test_scan_ports_ignores_unknown_updated_ports(self):
        # the port '5' was not seen on current ports. Hence it has either
        # never been wired or already removed and should be ignored
        self._test_scan_ports_with_updated_ports(set([4, 5]))

    def test_scan_ports_ignores_updated_port_if_removed(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        updated_ports = set([1, 2])
        expected = dict(current=vif_port_set, added=set([3]),
                        removed=set([2]), updated=set([1]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_no_vif_changes_returns_updated_port_only(self):
        vif_port_set = set([1, 2, 3])
        registered_ports = set([1, 2, 3])
        updated_ports = set([2])
        expected = dict(current=vif_port_set, updated=set([2]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_update_ports_returns_changed_vlan(self):
        br = ovs_lib.OVSBridge('br-int')
        mac = "ca:fe:de:ad:be:ef"
        port = ovs_lib.VifPort(1, 1, 1, mac, br)
        lvm = ovs_neutron_agent.LocalVLANMapping(
            1, '1', None, 1, {port.vif_id: port})
        local_vlan_map = {'1': lvm}
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        port_tags_dict = {1: []}
        expected = dict(
            added=set([3]), current=vif_port_set,
            removed=set([2]), updated=set([1])
        )
        with mock.patch.dict(self.agent.local_vlan_map, local_vlan_map):
            actual = self.mock_scan_ports(
                vif_port_set, registered_ports, port_tags_dict=port_tags_dict)
        self.assertEqual(expected, actual)

    def test_treat_devices_added_returns_raises_for_missing_device(self):
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              side_effect=Exception()),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=mock.Mock())):
            self.assertRaises(
                ovs_neutron_agent.DeviceListRetrievalError,
                self.agent.treat_devices_added_or_updated, [{}], False)

    def _mock_treat_devices_added_updated(self, details, port, func_name):
        """Mock treat devices added or updated.

        :param details: the details to return for the device
        :param port: the port that get_vif_port_by_id should return
        :param func_name: the function that should be called
        :returns: whether the named function was called
        """
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[details]),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=port),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, func_name)
        ) as (get_dev_fn, get_vif_func, upd_dev_up, upd_dev_down, func):
            skip_devs = self.agent.treat_devices_added_or_updated([{}], False)
            # The function should not raise
            self.assertFalse(skip_devs)
        return func.called

    def test_treat_devices_added_updated_ignores_invalid_ofport(self):
        port = mock.Mock()
        port.ofport = -1
        self.assertFalse(self._mock_treat_devices_added_updated(
            mock.MagicMock(), port, 'port_dead'))

    def test_treat_devices_added_updated_marks_unknown_port_as_dead(self):
        port = mock.Mock()
        port.ofport = 1
        self.assertTrue(self._mock_treat_devices_added_updated(
            mock.MagicMock(), port, 'port_dead'))

    def test_treat_devices_added_does_not_process_missing_port(self):
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details'),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=None)
        ) as (get_dev_fn, get_vif_func):
            self.assertFalse(get_dev_fn.called)

    def test_treat_devices_added_updated_updates_known_port(self):
        details = mock.MagicMock()
        details.__contains__.side_effect = lambda x: True
        self.assertTrue(self._mock_treat_devices_added_updated(
            details, mock.Mock(), 'treat_vif_port'))

    def test_treat_devices_added_updated_skips_if_port_not_found(self):
        dev_mock = mock.MagicMock()
        dev_mock.__getitem__.return_value = 'the_skipped_one'
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[dev_mock]),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=None),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, 'treat_vif_port')
        ) as (get_dev_fn, get_vif_func, upd_dev_up,
              upd_dev_down, treat_vif_port):
            skip_devs = self.agent.treat_devices_added_or_updated([{}], False)
            # The function should return False for resync and no device
            # processed
            self.assertEqual(['the_skipped_one'], skip_devs)
            self.assertFalse(treat_vif_port.called)
            self.assertFalse(upd_dev_down.called)
            self.assertFalse(upd_dev_up.called)

    def test_treat_devices_added_updated_put_port_down(self):
        fake_details_dict = {'admin_state_up': False,
                             'port_id': 'xxx',
                             'device': 'xxx',
                             'network_id': 'yyy',
                             'physical_network': 'foo',
                             'segmentation_id': 'bar',
                             'network_type': 'baz',
                             'fixed_ips': [{'subnet_id': 'my-subnet-uuid',
                                            'ip_address': '1.1.1.1'}],
                             'device_owner': 'compute:None'
                             }

        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc,
                              'get_devices_details_list',
                              return_value=[fake_details_dict]),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=mock.MagicMock()),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, 'treat_vif_port')
        ) as (get_dev_fn, get_vif_func, upd_dev_up,
              upd_dev_down, treat_vif_port):
            skip_devs = self.agent.treat_devices_added_or_updated([{}], False)
            # The function should return False for resync
            self.assertFalse(skip_devs)
            self.assertTrue(treat_vif_port.called)
            self.assertTrue(upd_dev_down.called)

    def test_treat_devices_removed_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               side_effect=Exception()):
            self.assertTrue(self.agent.treat_devices_removed([{}]))

    def _mock_treat_devices_removed(self, port_exists):
        details = dict(exists=port_exists)
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               return_value=details):
            with mock.patch.object(self.agent, 'port_unbound') as port_unbound:
                self.assertFalse(self.agent.treat_devices_removed([{}]))
        self.assertTrue(port_unbound.called)

    def test_treat_devices_removed_unbinds_port(self):
        self._mock_treat_devices_removed(True)

    def test_treat_devices_removed_ignores_missing_port(self):
        self._mock_treat_devices_removed(False)

    def _test_process_network_ports(self, port_info):
        with contextlib.nested(
            mock.patch.object(self.agent.sg_agent, "setup_port_filters"),
            mock.patch.object(self.agent, "treat_devices_added_or_updated",
                              return_value=[]),
            mock.patch.object(self.agent, "treat_devices_removed",
                              return_value=False)
        ) as (setup_port_filters, device_added_updated, device_removed):
            self.assertFalse(self.agent.process_network_ports(port_info,
                                                              False))
            setup_port_filters.assert_called_once_with(
                port_info['added'], port_info.get('updated', set()))
            device_added_updated.assert_called_once_with(
                port_info['added'] | port_info.get('updated', set()), False)
            device_removed.assert_called_once_with(port_info['removed'])

    def test_process_network_ports(self):
        self._test_process_network_ports(
            {'current': set(['tap0']),
             'removed': set(['eth0']),
             'added': set(['eth1'])})

    def test_process_network_port_with_updated_ports(self):
        self._test_process_network_ports(
            {'current': set(['tap0', 'tap1']),
             'updated': set(['tap1', 'eth1']),
             'removed': set(['eth0']),
             'added': set(['eth1'])})

    def test_report_state(self):
        with mock.patch.object(self.agent.state_rpc,
                               "report_state") as report_st:
            self.agent.int_br_device_count = 5
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, True)
            self.assertNotIn("start_flag", self.agent.agent_state)
            self.assertFalse(self.agent.use_call)
            self.assertEqual(
                self.agent.agent_state["configurations"]["devices"],
                self.agent.int_br_device_count
            )
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, False)

    def test_report_state_fail(self):
        with mock.patch.object(self.agent.state_rpc,
                               "report_state") as report_st:
            report_st.side_effect = Exception()
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, True)
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state, True)

    def test_network_delete(self):
        with contextlib.nested(
            mock.patch.object(self.agent, "reclaim_local_vlan"),
            mock.patch.object(self.agent.tun_br, "cleanup_tunnel_port")
        ) as (recl_fn, clean_tun_fn):
            self.agent.network_delete("unused_context",
                                      network_id="123")
            self.assertFalse(recl_fn.called)
            self.agent.local_vlan_map["123"] = "LVM object"
            self.agent.network_delete("unused_context",
                                      network_id="123")
            self.assertFalse(clean_tun_fn.called)
            recl_fn.assert_called_with("123")

    def test_port_update(self):
        port = {"id": "123",
                "network_id": "124",
                "admin_state_up": False}
        self.agent.port_update("unused_context",
                               port=port,
                               network_type="vlan",
                               segmentation_id="1",
                               physical_network="physnet")
        self.assertEqual(set(['123']), self.agent.updated_ports)

    def test_port_delete(self):
        port_id = "123"
        port_name = "foo"
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=mock.MagicMock(
                                      port_name=port_name)),
            mock.patch.object(self.agent.int_br, "delete_port")
        ) as (get_vif_func, del_port_func):
            self.agent.port_delete("unused_context",
                                   port_id=port_id)
            self.assertTrue(get_vif_func.called)
            del_port_func.assert_called_once_with(port_name)

    def test_setup_physical_bridges(self):
        with contextlib.nested(
            mock.patch.object(ip_lib, "device_exists"),
            mock.patch.object(sys, "exit"),
            mock.patch.object(utils, "execute"),
            mock.patch.object(ovs_lib.OVSBridge, "remove_all_flows"),
            mock.patch.object(ovs_lib.OVSBridge, "add_flow"),
            mock.patch.object(ovs_lib.OVSBridge, "add_patch_port"),
            mock.patch.object(ovs_lib.OVSBridge, "delete_port"),
            mock.patch.object(ovs_lib.OVSBridge, "set_db_attribute"),
            mock.patch.object(self.agent.int_br, "add_flow"),
            mock.patch.object(self.agent.int_br, "add_patch_port"),
            mock.patch.object(self.agent.int_br, "delete_port"),
            mock.patch.object(self.agent.int_br, "set_db_attribute"),
        ) as (devex_fn, sysexit_fn, utilsexec_fn, remflows_fn, ovs_add_flow_fn,
              ovs_addpatch_port_fn, ovs_delport_fn, ovs_set_attr_fn,
              br_add_flow_fn, br_addpatch_port_fn, br_delport_fn,
              br_set_attr_fn):
            devex_fn.return_value = True
            parent = mock.MagicMock()
            parent.attach_mock(ovs_addpatch_port_fn, 'phy_add_patch_port')
            parent.attach_mock(ovs_add_flow_fn, 'phy_add_flow')
            parent.attach_mock(ovs_set_attr_fn, 'phy_set_attr')
            parent.attach_mock(br_addpatch_port_fn, 'int_add_patch_port')
            parent.attach_mock(br_add_flow_fn, 'int_add_flow')
            parent.attach_mock(br_set_attr_fn, 'int_set_attr')

            ovs_addpatch_port_fn.return_value = "phy_ofport"
            br_addpatch_port_fn.return_value = "int_ofport"
            self.agent.setup_physical_bridges({"physnet1": "br-eth"})
            expected_calls = [
                mock.call.phy_add_flow(priority=1, actions='normal'),
                mock.call.int_add_patch_port('int-br-eth',
                                             constants.NONEXISTENT_PEER),
                mock.call.phy_add_patch_port('phy-br-eth',
                                             constants.NONEXISTENT_PEER),
                mock.call.int_add_flow(priority=2, in_port='int_ofport',
                                       actions='drop'),
                mock.call.phy_add_flow(priority=2, in_port='phy_ofport',
                                       actions='drop'),
                mock.call.int_set_attr('Interface', 'int-br-eth',
                                       'options:peer', 'phy-br-eth'),
                mock.call.phy_set_attr('Interface', 'phy-br-eth',
                                       'options:peer', 'int-br-eth'),

            ]
            parent.assert_has_calls(expected_calls)
            self.assertEqual(self.agent.int_ofports["physnet1"],
                             "int_ofport")
            self.assertEqual(self.agent.phys_ofports["physnet1"],
                             "phy_ofport")

    def test_setup_physical_bridges_using_veth_interconnection(self):
        self.agent.use_veth_interconnection = True
        with contextlib.nested(
            mock.patch.object(ip_lib, "device_exists"),
            mock.patch.object(sys, "exit"),
            mock.patch.object(utils, "execute"),
            mock.patch.object(ovs_lib.OVSBridge, "remove_all_flows"),
            mock.patch.object(ovs_lib.OVSBridge, "add_flow"),
            mock.patch.object(ovs_lib.OVSBridge, "add_port"),
            mock.patch.object(ovs_lib.OVSBridge, "delete_port"),
            mock.patch.object(self.agent.int_br, "add_port"),
            mock.patch.object(self.agent.int_br, "delete_port"),
            mock.patch.object(ip_lib.IPWrapper, "add_veth"),
            mock.patch.object(ip_lib.IpLinkCommand, "delete"),
            mock.patch.object(ip_lib.IpLinkCommand, "set_up"),
            mock.patch.object(ip_lib.IpLinkCommand, "set_mtu"),
            mock.patch.object(ovs_lib.BaseOVS, "get_bridges")
        ) as (devex_fn, sysexit_fn, utilsexec_fn, remflows_fn, ovs_addfl_fn,
              ovs_addport_fn, ovs_delport_fn, br_addport_fn, br_delport_fn,
              addveth_fn, linkdel_fn, linkset_fn, linkmtu_fn, get_br_fn):
            devex_fn.return_value = True
            parent = mock.MagicMock()
            parent.attach_mock(utilsexec_fn, 'utils_execute')
            parent.attach_mock(linkdel_fn, 'link_delete')
            parent.attach_mock(addveth_fn, 'add_veth')
            addveth_fn.return_value = (ip_lib.IPDevice("int-br-eth1"),
                                       ip_lib.IPDevice("phy-br-eth1"))
            ovs_addport_fn.return_value = "phys_veth_ofport"
            br_addport_fn.return_value = "int_veth_ofport"
            get_br_fn.return_value = ["br-eth"]
            self.agent.setup_physical_bridges({"physnet1": "br-eth"})
            expected_calls = [mock.call.link_delete(),
                              mock.call.utils_execute(['udevadm',
                                                       'settle',
                                                       '--timeout=10']),
                              mock.call.add_veth('int-br-eth',
                                                 'phy-br-eth')]
            parent.assert_has_calls(expected_calls, any_order=False)
            self.assertEqual(self.agent.int_ofports["physnet1"],
                             "int_veth_ofport")
            self.assertEqual(self.agent.phys_ofports["physnet1"],
                             "phys_veth_ofport")

    def test_get_peer_name(self):
            bridge1 = "A_REALLY_LONG_BRIDGE_NAME1"
            bridge2 = "A_REALLY_LONG_BRIDGE_NAME2"
            self.agent.use_veth_interconnection = True
            self.assertEqual(len(self.agent.get_peer_name('int-', bridge1)),
                             n_const.DEVICE_NAME_MAX_LEN)
            self.assertEqual(len(self.agent.get_peer_name('int-', bridge2)),
                             n_const.DEVICE_NAME_MAX_LEN)
            self.assertNotEqual(self.agent.get_peer_name('int-', bridge1),
                                self.agent.get_peer_name('int-', bridge2))

    def test_setup_tunnel_br(self):
        self.tun_br = mock.Mock()
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, "add_patch_port",
                              return_value=1),
            mock.patch.object(self.agent.tun_br, "add_patch_port",
                              return_value=2),
            mock.patch.object(self.agent.tun_br, "remove_all_flows"),
            mock.patch.object(self.agent.tun_br, "add_flow"),
            mock.patch.object(ovs_lib, "OVSBridge"),
            mock.patch.object(self.agent.tun_br, "reset_bridge"),
            mock.patch.object(sys, "exit")
        ) as (intbr_patch_fn, tunbr_patch_fn, remove_all_fn,
              add_flow_fn, ovs_br_fn, reset_br_fn, exit_fn):
            self.agent.reset_tunnel_br(None)
            self.agent.setup_tunnel_br()
            self.assertTrue(intbr_patch_fn.called)

    def test_setup_tunnel_port(self):
        self.agent.tun_br = mock.Mock()
        self.agent.l2_pop = False
        self.agent.udp_vxlan_port = 8472
        self.agent.tun_br_ofports['vxlan'] = {}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, "add_tunnel_port",
                              return_value='6'),
            mock.patch.object(self.agent.tun_br, "add_flow")
        ) as (add_tun_port_fn, add_flow_fn):
            self.agent._setup_tunnel_port(self.agent.tun_br, 'portname',
                                          '1.2.3.4', 'vxlan')
            self.assertTrue(add_tun_port_fn.called)

    def test_port_unbound(self):
        with mock.patch.object(self.agent, "reclaim_local_vlan") as reclvl_fn:
            self.agent.enable_tunneling = True
            lvm = mock.Mock()
            lvm.network_type = "gre"
            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.local_vlan_map["netuid12345"] = lvm
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertTrue(reclvl_fn.called)
            reclvl_fn.called = False

            lvm.vif_ports = {}
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.port_unbound("vif3", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

    def _prepare_l2_pop_ofports(self):
        lvm1 = mock.Mock()
        lvm1.network_type = 'gre'
        lvm1.vlan = 'vlan1'
        lvm1.segmentation_id = 'seg1'
        lvm1.tun_ofports = set(['1'])
        lvm2 = mock.Mock()
        lvm2.network_type = 'gre'
        lvm2.vlan = 'vlan2'
        lvm2.segmentation_id = 'seg2'
        lvm2.tun_ofports = set(['1', '2'])
        self.agent.local_vlan_map = {'net1': lvm1, 'net2': lvm2}
        self.agent.tun_br_ofports = {'gre':
                                     {'1.1.1.1': '1', '2.2.2.2': '2'}}
        self.agent.arp_responder_enabled = True

    def test_fdb_ignore_network(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net3': {}}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_flow'),
            mock.patch.object(self.agent.tun_br, 'delete_flows'),
            mock.patch.object(self.agent, '_setup_tunnel_port'),
            mock.patch.object(self.agent, 'cleanup_tunnel_port')
        ) as (add_flow_fn, del_flow_fn, add_tun_fn, clean_tun_fn):
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_flow_fn.called)
            self.assertFalse(add_tun_fn.called)
            self.agent.fdb_remove(None, fdb_entry)
            self.assertFalse(del_flow_fn.called)
            self.assertFalse(clean_tun_fn.called)

    def test_fdb_ignore_self(self):
        self._prepare_l2_pop_ofports()
        self.agent.local_ip = 'agent_ip'
        fdb_entry = {'net2':
                     {'network_type': 'gre',
                      'segment_id': 'tun2',
                      'ports':
                      {'agent_ip':
                       [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1),
                        n_const.FLOODING_ENTRY]}}}
        with mock.patch.object(self.agent.tun_br,
                               "deferred") as defer_fn:
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(defer_fn.called)

            self.agent.fdb_remove(None, fdb_entry)
            self.assertFalse(defer_fn.called)

    def test_fdb_add_flows(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net1':
                     {'network_type': 'gre',
                      'segment_id': 'tun1',
                      'ports':
                      {'2.2.2.2':
                       [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1),
                        n_const.FLOODING_ENTRY]}}}

        class ActionMatcher(object):
            def __init__(self, action_str):
                self.ordered = self.order_ports(action_str)

            def order_ports(self, action_str):
                halves = action_str.split('output:')
                ports = sorted(halves.pop().split(','))
                halves.append(','.join(ports))
                return 'output:'.join(halves)

            def __eq__(self, other):
                return self.ordered == self.order_ports(other)

        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'deferred'),
            mock.patch.object(self.agent.tun_br, 'do_action_flows'),
            mock.patch.object(self.agent, '_setup_tunnel_port'),
        ) as (deferred_fn, do_action_flows_fn, add_tun_fn):
            deferred_fn.return_value = ovs_lib.DeferredOVSBridge(
                self.agent.tun_br)
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_tun_fn.called)
            actions = (constants.ARP_RESPONDER_ACTIONS %
                       {'mac': netaddr.EUI(FAKE_MAC, dialect=netaddr.mac_unix),
                        'ip': netaddr.IPAddress(FAKE_IP1)})
            expected_calls = [
                mock.call('add', [dict(table=constants.ARP_RESPONDER,
                                       priority=1,
                                       proto='arp',
                                       dl_vlan='vlan1',
                                       nw_dst=FAKE_IP1,
                                       actions=actions),
                                  dict(table=constants.UCAST_TO_TUN,
                                       priority=2,
                                       dl_vlan='vlan1',
                                       dl_dst=FAKE_MAC,
                                       actions='strip_vlan,'
                                       'set_tunnel:seg1,output:2')]),
                mock.call('mod', [dict(table=constants.FLOOD_TO_TUN,
                                       dl_vlan='vlan1',
                                       actions=ActionMatcher('strip_vlan,'
                                       'set_tunnel:seg1,output:1,2'))]),
            ]
            do_action_flows_fn.assert_has_calls(expected_calls)

    def test_fdb_del_flows(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net2':
                     {'network_type': 'gre',
                      'segment_id': 'tun2',
                      'ports':
                      {'2.2.2.2':
                       [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1),
                        n_const.FLOODING_ENTRY]}}}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'deferred'),
            mock.patch.object(self.agent.tun_br, 'do_action_flows'),
        ) as (deferred_fn, do_action_flows_fn):
            deferred_fn.return_value = ovs_lib.DeferredOVSBridge(
                self.agent.tun_br)
            self.agent.fdb_remove(None, fdb_entry)
            expected_calls = [
                mock.call('mod', [dict(table=constants.FLOOD_TO_TUN,
                                       dl_vlan='vlan2',
                                       actions='strip_vlan,'
                                       'set_tunnel:seg2,output:1')]),
                mock.call('del', [dict(table=constants.ARP_RESPONDER,
                                       proto='arp',
                                       dl_vlan='vlan2',
                                       nw_dst=FAKE_IP1),
                                  dict(table=constants.UCAST_TO_TUN,
                                       dl_vlan='vlan2',
                                       dl_dst=FAKE_MAC),
                                  dict(in_port='2')]),
            ]
            do_action_flows_fn.assert_has_calls(expected_calls)

    def test_fdb_add_port(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net1':
                     {'network_type': 'gre',
                      'segment_id': 'tun1',
                      'ports': {'1.1.1.1': [l2pop_rpc.PortInfo(FAKE_MAC,
                                                               FAKE_IP1)]}}}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'deferred'),
            mock.patch.object(self.agent.tun_br, 'do_action_flows'),
            mock.patch.object(self.agent, '_setup_tunnel_port')
        ) as (deferred_fn, do_action_flows_fn, add_tun_fn):
            deferred_br = ovs_lib.DeferredOVSBridge(self.agent.tun_br)
            deferred_fn.return_value = deferred_br
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_tun_fn.called)
            fdb_entry['net1']['ports']['10.10.10.10'] = [
                l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1)]
            self.agent.fdb_add(None, fdb_entry)
            add_tun_fn.assert_called_with(
                deferred_br, 'gre-0a0a0a0a', '10.10.10.10', 'gre')

    def test_fdb_del_port(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net2':
                     {'network_type': 'gre',
                      'segment_id': 'tun2',
                      'ports': {'2.2.2.2': [n_const.FLOODING_ENTRY]}}}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'deferred'),
            mock.patch.object(self.agent.tun_br, 'do_action_flows'),
            mock.patch.object(self.agent.tun_br, 'delete_port')
        ) as (deferred_fn, do_action_flows_fn, delete_port_fn):
            deferred_br = ovs_lib.DeferredOVSBridge(self.agent.tun_br)
            deferred_fn.return_value = deferred_br
            self.agent.fdb_remove(None, fdb_entry)
            delete_port_fn.assert_called_once_with('gre-02020202')

    def test_fdb_update_chg_ip(self):
        self._prepare_l2_pop_ofports()
        fdb_entries = {'chg_ip':
                       {'net1':
                        {'agent_ip':
                         {'before': [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP1)],
                          'after': [l2pop_rpc.PortInfo(FAKE_MAC, FAKE_IP2)]}}}}
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'deferred'),
            mock.patch.object(self.agent.tun_br, 'do_action_flows'),
        ) as (deferred_fn, do_action_flows_fn):
            deferred_br = ovs_lib.DeferredOVSBridge(self.agent.tun_br)
            deferred_fn.return_value = deferred_br
            self.agent.fdb_update(None, fdb_entries)
            actions = (constants.ARP_RESPONDER_ACTIONS %
                       {'mac': netaddr.EUI(FAKE_MAC, dialect=netaddr.mac_unix),
                        'ip': netaddr.IPAddress(FAKE_IP2)})
            expected_calls = [
                mock.call('add', [dict(table=constants.ARP_RESPONDER,
                                       priority=1,
                                       proto='arp',
                                       dl_vlan='vlan1',
                                       nw_dst=FAKE_IP2,
                                       actions=actions)]),
                mock.call('del', [dict(table=constants.ARP_RESPONDER,
                                       proto='arp',
                                       dl_vlan='vlan1',
                                       nw_dst=FAKE_IP1)])
            ]
            do_action_flows_fn.assert_has_calls(expected_calls)
            self.assertEqual(len(expected_calls),
                             len(do_action_flows_fn.mock_calls))

    def test_del_fdb_flow_idempotency(self):
        lvm = mock.Mock()
        lvm.network_type = 'gre'
        lvm.vlan = 'vlan1'
        lvm.segmentation_id = 'seg1'
        lvm.tun_ofports = set(['1', '2'])
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'mod_flow'),
            mock.patch.object(self.agent.tun_br, 'delete_flows')
        ) as (mod_flow_fn, delete_flows_fn):
            self.agent.del_fdb_flow(self.agent.tun_br, n_const.FLOODING_ENTRY,
                                    '1.1.1.1', lvm, '3')
            self.assertFalse(mod_flow_fn.called)
            self.assertFalse(delete_flows_fn.called)

    def test_recl_lv_port_to_preserve(self):
        self._prepare_l2_pop_ofports()
        self.agent.l2_pop = True
        self.agent.enable_tunneling = True
        with mock.patch.object(
            self.agent.tun_br, 'cleanup_tunnel_port'
        ) as clean_tun_fn:
            self.agent.reclaim_local_vlan('net1')
            self.assertFalse(clean_tun_fn.called)

    def test_recl_lv_port_to_remove(self):
        self._prepare_l2_pop_ofports()
        self.agent.l2_pop = True
        self.agent.enable_tunneling = True
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'delete_port'),
            mock.patch.object(self.agent.tun_br, 'delete_flows')
        ) as (del_port_fn, del_flow_fn):
            self.agent.reclaim_local_vlan('net2')
            del_port_fn.assert_called_once_with('gre-02020202')

    def test_daemon_loop_uses_polling_manager(self):
        with mock.patch(
            'neutron.agent.linux.polling.get_polling_manager') as mock_get_pm:
            with mock.patch.object(self.agent, 'rpc_loop') as mock_loop:
                self.agent.daemon_loop()
        mock_get_pm.assert_called_with(True,
                                       constants.DEFAULT_OVSDBMON_RESPAWN)
        mock_loop.assert_called_once_with(polling_manager=mock.ANY)

    def test_setup_tunnel_port_invalid_ofport(self):
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_tunnel_port',
                              return_value=ovs_lib.INVALID_OFPORT),
            mock.patch.object(ovs_neutron_agent.LOG, 'error')
        ) as (add_tunnel_port_fn, log_error_fn):
            ofport = self.agent._setup_tunnel_port(
                self.agent.tun_br, 'gre-1', 'remote_ip', p_const.TYPE_GRE)
            add_tunnel_port_fn.assert_called_once_with(
                'gre-1', 'remote_ip', self.agent.local_ip, p_const.TYPE_GRE,
                self.agent.vxlan_udp_port, self.agent.dont_fragment)
            log_error_fn.assert_called_once_with(
                _("Failed to set-up %(type)s tunnel port to %(ip)s"),
                {'type': p_const.TYPE_GRE, 'ip': 'remote_ip'})
            self.assertEqual(ofport, 0)

    def test_setup_tunnel_port_error_negative_df_disabled(self):
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, 'add_tunnel_port',
                              return_value=ovs_lib.INVALID_OFPORT),
            mock.patch.object(ovs_neutron_agent.LOG, 'error')
        ) as (add_tunnel_port_fn, log_error_fn):
            self.agent.dont_fragment = False
            ofport = self.agent._setup_tunnel_port(
                self.agent.tun_br, 'gre-1', 'remote_ip', p_const.TYPE_GRE)
            add_tunnel_port_fn.assert_called_once_with(
                'gre-1', 'remote_ip', self.agent.local_ip, p_const.TYPE_GRE,
                self.agent.vxlan_udp_port, self.agent.dont_fragment)
            log_error_fn.assert_called_once_with(
                _("Failed to set-up %(type)s tunnel port to %(ip)s"),
                {'type': p_const.TYPE_GRE, 'ip': 'remote_ip'})
            self.assertEqual(ofport, 0)

    def test_tunnel_sync_with_ml2_plugin(self):
        fake_tunnel_details = {'tunnels': [{'ip_address': '100.101.31.15'}]}
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'tunnel_sync',
                              return_value=fake_tunnel_details),
            mock.patch.object(self.agent, '_setup_tunnel_port')
        ) as (tunnel_sync_rpc_fn, _setup_tunnel_port_fn):
            self.agent.tunnel_types = ['vxlan']
            self.agent.tunnel_sync()
            expected_calls = [mock.call(self.agent.tun_br, 'vxlan-64651f0f',
                                        '100.101.31.15', 'vxlan')]
            _setup_tunnel_port_fn.assert_has_calls(expected_calls)

    def test_tunnel_sync_invalid_ip_address(self):
        fake_tunnel_details = {'tunnels': [{'ip_address': '300.300.300.300'},
                                           {'ip_address': '100.100.100.100'}]}
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'tunnel_sync',
                              return_value=fake_tunnel_details),
            mock.patch.object(self.agent, '_setup_tunnel_port')
        ) as (tunnel_sync_rpc_fn, _setup_tunnel_port_fn):
            self.agent.tunnel_types = ['vxlan']
            self.agent.tunnel_sync()
            _setup_tunnel_port_fn.assert_called_once_with(self.agent.tun_br,
                                                          'vxlan-64646464',
                                                          '100.100.100.100',
                                                          'vxlan')

    def test_tunnel_update(self):
        kwargs = {'tunnel_ip': '10.10.10.10',
                  'tunnel_type': 'gre'}
        self.agent._setup_tunnel_port = mock.Mock()
        self.agent.enable_tunneling = True
        self.agent.tunnel_types = ['gre']
        self.agent.l2_pop = False
        self.agent.tunnel_update(context=None, **kwargs)
        expected_calls = [
            mock.call(self.agent.tun_br, 'gre-0a0a0a0a', '10.10.10.10', 'gre')]
        self.agent._setup_tunnel_port.assert_has_calls(expected_calls)

    def test_tunnel_delete(self):
        kwargs = {'tunnel_ip': '10.10.10.10',
                  'tunnel_type': 'gre'}
        self.agent.enable_tunneling = True
        self.agent.tunnel_types = ['gre']
        self.agent.tun_br_ofports = {'gre': {'10.10.10.10': '1'}}
        with mock.patch.object(
            self.agent, 'cleanup_tunnel_port'
        ) as clean_tun_fn:
            self.agent.tunnel_delete(context=None, **kwargs)
            self.assertTrue(clean_tun_fn.called)

    def test_ovs_status(self):
        reply2 = {'current': set(['tap0']),
                  'added': set(['tap2']),
                  'removed': set([])}

        reply3 = {'current': set(['tap2']),
                  'added': set([]),
                  'removed': set(['tap0'])}

        with contextlib.nested(
            mock.patch.object(async_process.AsyncProcess, "_spawn"),
            mock.patch.object(log.KeywordArgumentAdapter, 'exception'),
            mock.patch.object(ovs_neutron_agent.OVSNeutronAgent,
                              'scan_ports'),
            mock.patch.object(ovs_neutron_agent.OVSNeutronAgent,
                              'process_network_ports'),
            mock.patch.object(ovs_neutron_agent.OVSNeutronAgent,
                              'check_ovs_status'),
            mock.patch.object(ovs_neutron_agent.OVSNeutronAgent,
                              'setup_integration_br'),
            mock.patch.object(ovs_neutron_agent.OVSNeutronAgent,
                              'setup_physical_bridges'),
            mock.patch.object(time, 'sleep')
        ) as (spawn_fn, log_exception, scan_ports, process_network_ports,
              check_ovs_status, setup_int_br, setup_phys_br, time_sleep):
            log_exception.side_effect = Exception(
                'Fake exception to get out of the loop')
            scan_ports.side_effect = [reply2, reply3]
            process_network_ports.side_effect = [
                False, Exception('Fake exception to get out of the loop')]
            check_ovs_status.side_effect = [constants.OVS_NORMAL,
                                            constants.OVS_DEAD,
                                            constants.OVS_RESTARTED]

            # This will exit after the third loop
            try:
                self.agent.daemon_loop()
            except Exception:
                pass

        scan_ports.assert_has_calls([
            mock.call(set(), set()),
            mock.call(set(), set())
        ])
        process_network_ports.assert_has_calls([
            mock.call({'current': set(['tap0']),
                       'removed': set([]),
                       'added': set(['tap2'])}, False),
            mock.call({'current': set(['tap2']),
                       'removed': set(['tap0']),
                       'added': set([])}, True)
        ])

        # Verify the second time through the loop we triggered an
        # OVS restart and re-setup the bridges
        setup_int_br.assert_has_calls([mock.call()])
        setup_phys_br.assert_has_calls([mock.call({})])

    def test_set_rpc_timeout(self):
        self.agent._handle_sigterm(None, None)
        for rpc_client in (self.agent.plugin_rpc.client,
                           self.agent.sg_plugin_rpc.client,
                           self.agent.dvr_plugin_rpc.client,
                           self.agent.state_rpc.client):
            self.assertEqual(10, rpc_client.timeout)

    def test_set_rpc_timeout_no_value(self):
        self.agent.quitting_rpc_timeout = None
        with mock.patch.object(self.agent, 'set_rpc_timeout') as mock_set_rpc:
            self.agent._handle_sigterm(None, None)
        self.assertFalse(mock_set_rpc.called)


class AncillaryBridgesTest(base.BaseTestCase):

    def setUp(self):
        super(AncillaryBridgesTest, self).setUp()
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        cfg.CONF.set_default('firewall_driver',
                             'neutron.agent.firewall.NoopFirewallDriver',
                             group='SECURITYGROUP')
        cfg.CONF.set_override('report_interval', 0, 'AGENT')
        self.kwargs = ovs_neutron_agent.create_agent_config_map(cfg.CONF)

    def _test_ancillary_bridges(self, bridges, ancillary):
        device_ids = ancillary[:]

        def pullup_side_effect(*args):
            # Check that the device_id exists, if it does return it
            # if it does not return None
            try:
                device_ids.remove(args[0])
                return args[0]
            except Exception:
                return None

        with contextlib.nested(
            mock.patch('neutron.plugins.openvswitch.agent.ovs_neutron_agent.'
                       'OVSNeutronAgent.setup_integration_br'),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'get_local_port_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.linux.ovs_lib.OVSBridge.'
                       'set_secure_mode'),
            mock.patch('neutron.agent.linux.ovs_lib.BaseOVS.get_bridges',
                       return_value=bridges),
            mock.patch('neutron.agent.linux.ovs_lib.BaseOVS.'
                       'get_bridge_external_bridge_id',
                       side_effect=pullup_side_effect)):
            self.agent = ovs_neutron_agent.OVSNeutronAgent(**self.kwargs)
            self.assertEqual(len(ancillary), len(self.agent.ancillary_brs))
            if ancillary:
                bridges = [br.br_name for br in self.agent.ancillary_brs]
                for br in ancillary:
                    self.assertIn(br, bridges)

    def test_ancillary_bridges_single(self):
        bridges = ['br-int', 'br-ex']
        self._test_ancillary_bridges(bridges, ['br-ex'])

    def test_ancillary_bridges_none(self):
        bridges = ['br-int']
        self._test_ancillary_bridges(bridges, [])

    def test_ancillary_bridges_multiple(self):
        bridges = ['br-int', 'br-ex1', 'br-ex2']
        self._test_ancillary_bridges(bridges, ['br-ex1', 'br-ex2'])
