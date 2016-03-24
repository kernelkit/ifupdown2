#!/usr/bin/python
#
# Copyright 2014 Cumulus Networks, Inc. All rights reserved.
# Author: Roopa Prabhu, roopa@cumulusnetworks.com
#

import os
import atexit
from ifupdown.iface import *
import ifupdown.policymanager as policymanager
import ifupdownaddons
import ifupdown.rtnetlink_api as rtnetlink_api
from ifupdownaddons.modulebase import moduleBase
from ifupdownaddons.bondutil import bondutil
from ifupdownaddons.iproute2 import iproute2
from ifupdownaddons.dhclient import dhclient

class vrfPrivFlags:
    PROCESSED = 0x1

class vrf(moduleBase):
    """  ifupdown2 addon module to configure vrfs """
    _modinfo = { 'mhelp' : 'vrf configuration module',
                    'attrs' : {
                    'vrf-table':
                         {'help' : 'vrf device table id. key to ' +
                                   'creating a vrf device',
                          'example': ['vrf-table-id 1']},
                    'vrf-default-route':
                         {'help' : 'vrf device default route ' +
                                   'to avoid communication outside the vrf device',
                          'example': ['vrf-default-route yes/no']},
                    'vrf':
                         {'help' : 'vrf the interface is part of.',
                          'example': ['vrf blue']}}}

    iproute2_vrf_filename = '/etc/iproute2/rt_tables.d/ifupdown2_vrf_map.conf'
    iproute2_vrf_filehdr = '# This file is autogenerated by ifupdown2.\n' + \
                           '# It contains the vrf name to table mapping.\n' + \
                           '# Reserved table range %s %s\n'
    VRF_TABLE_START = 1001
    VRF_TABLE_END = 5000

    def __init__(self, *args, **kargs):
        ifupdownaddons.modulebase.moduleBase.__init__(self, *args, **kargs)
        self.ipcmd = None
        self.bondcmd = None
        self.dhclientcmd = None
        self.name = self.__class__.__name__
        if self.PERFMODE:
            # if perf mode is set, remove vrf map file.
            # start afresh. PERFMODE is set at boot
            if os.path.exists(self.iproute2_vrf_filename):
                try:
                    self.logger.info('vrf: removing file %s'
                                     %self.iproute2_vrf_filename)
                    os.remove(self.iproute2_vrf_filename)
                except Exception, e:
                    self.logger.debug('vrf: removing file failed (%s)'
                                      %str(e))
        try:
            ip_rules = self.exec_command('/sbin/ip rule show').splitlines()
            self.ip_rule_cache = [' '.join(r.split()) for r in ip_rules]
        except Exception, e:
            self.ip_rule_cache = []
            self.logger.warn('%s' %str(e))

        try:
            ip_rules = self.exec_command('/sbin/ip -6 rule show').splitlines()
            self.ip6_rule_cache = [' '.join(r.split()) for r in ip_rules]
        except Exception, e:
            self.ip6_rule_cache = []
            self.logger.warn('%s' %str(e))

        #self.logger.debug("vrf: ip rule cache")
        #self.logger.info(self.ip_rule_cache)

        #self.logger.info("vrf: ip -6 rule cache")
        #self.logger.info(self.ip6_rule_cache)

        # XXX: check for vrf reserved overlap in /etc/iproute2/rt_tables
        self.iproute2_vrf_map = {}
        # read or create /etc/iproute2/rt_tables.d/ifupdown2.vrf_map
        if os.path.exists(self.iproute2_vrf_filename):
            self.vrf_map_fd = open(self.iproute2_vrf_filename, 'a+')
            lines = self.vrf_map_fd.readlines()
            for l in lines:
                l = l.strip()
                if l[0] == '#':
                    continue
                try:
                    (table, vrf_name) = l.strip().split()
                    self.iproute2_vrf_map[table] = vrf_name
                except Exception, e:
                    self.logger.info('vrf: iproute2_vrf_map: unable to parse %s'
                                     %l)
                    pass
        #self.logger.info("vrf: dumping iproute2_vrf_map")
        #self.logger.info(self.iproute2_vrf_map)

        # purge vrf table entries that are not around
        iproute2_vrf_map_pruned = {}
        for t, v in self.iproute2_vrf_map.iteritems():
            if os.path.exists('/sys/class/net/%s' %v):
                iproute2_vrf_map_pruned[int(t)] = v
            else:
                try:
                    # cleanup rules
                    self._del_vrf_rules(v, t)
                except Exception:
                    pass
        self.iproute2_vrf_map = iproute2_vrf_map_pruned

        self.vrf_table_id_start = policymanager.policymanager_api.get_module_globals(module_name=self.__class__.__name__, attr='vrf-table-id-start')
        if not self.vrf_table_id_start:
            self.vrf_table_id_start = self.VRF_TABLE_START
        self.vrf_table_id_end = policymanager.policymanager_api.get_module_globals(module_name=self.__class__.__name__, attr='vrf-table-id-end')
        if not self.vrf_table_id_end:
            self.vrf_table_id_end = self.VRF_TABLE_END
        self.vrf_max_count = policymanager.policymanager_api.get_module_globals(module_name=self.__class__.__name__, attr='vrf-max-count')

        last_used_vrf_table = None
        for t in range(self.vrf_table_id_start,
                       self.vrf_table_id_end):
            if not self.iproute2_vrf_map.get(t):
                break
            last_used_vrf_table = t
        self.last_used_vrf_table = last_used_vrf_table

        self.iproute2_write_vrf_map = False
        atexit.register(self.iproute2_vrf_map_write)
        self.vrf_fix_local_table = True
        self.vrf_count = 0
        self.vrf_cgroup_create = policymanager.policymanager_api.get_module_globals(module_name=self.__class__.__name__, attr='vrf-cgroup-create')
        if not self.vrf_cgroup_create:
            self.vrf_cgroup_create = False
        elif self.vrf_cgroup_create == 'yes':
            self.vrf_cgroup_create = True
        else:
            self.vrf_cgroup_create = False

    def iproute2_vrf_map_write(self):
        if not self.iproute2_write_vrf_map:
            return
        self.logger.info('vrf: writing table map to %s'
                         %self.iproute2_vrf_filename)
        with open(self.iproute2_vrf_filename, 'w') as f:
            f.write(self.iproute2_vrf_filehdr %(self.vrf_table_id_start,
                    self.vrf_table_id_end))
            for t, v in self.iproute2_vrf_map.iteritems():
                f.write('%s %s\n' %(t, v))

    def _is_vrf(self, ifaceobj):
        if ifaceobj.get_attr_value_first('vrf-table'):
            return True
        return False

    def get_upper_ifacenames(self, ifaceobj, ifacenames_all=None):
        """ Returns list of interfaces dependent on ifaceobj """

        vrf_table = ifaceobj.get_attr_value_first('vrf-table')
        if vrf_table:
            ifaceobj.link_type = ifaceLinkType.LINK_MASTER
            ifaceobj.link_kind |= ifaceLinkKind.VRF
        vrf_iface_name = ifaceobj.get_attr_value_first('vrf')
        if not vrf_iface_name:
            return None
        ifaceobj.link_type = ifaceLinkType.LINK_SLAVE
        ifaceobj.link_kind |= ifaceLinkKind.VRF_SLAVE

        return [vrf_iface_name]

    def get_upper_ifacenames_running(self, ifaceobj):
        return None

    def _get_iproute2_vrf_table(self, vrf_dev_name):
        for t, v in self.iproute2_vrf_map.iteritems():
            if v == vrf_dev_name:
                return str(t)
        return None

    def _get_avail_vrf_table_id(self):
        if self.last_used_vrf_table == None:
            table_id_start = self.vrf_table_id_start
        else:
            table_id_start = self.last_used_vrf_table + 1
        for t in range(table_id_start,
                       self.vrf_table_id_end):
            if not self.iproute2_vrf_map.get(t):
                self.last_used_vrf_table = t
                return str(t)
        return None

    def _iproute2_vrf_table_entry_add(self, vrf_dev_name, table_id):
        self.iproute2_vrf_map[int(table_id)] = vrf_dev_name
        self.iproute2_write_vrf_map = True

    def _iproute2_vrf_table_entry_del(self, table_id):
        try:
            del self.iproute2_vrf_map[int(table_id)]
            self.iproute2_write_vrf_map = True
        except Exception, e:
            self.logger.info('vrf: iproute2 vrf map del failed for %d (%s)'
                             %(table_id, str(e)))
            pass

    def _is_dhcp_slave(self, ifaceobj):
        if (not ifaceobj.addr_method or
            (ifaceobj.addr_method != 'dhcp' and
             ifaceobj.addr_method != 'dhcp6')):
                return False
        return True

    def _handle_dhcp_slaves(self, ifacename, vrfname, ifaceobj,
                            ifaceobj_getfunc):
        """ If we have a vrf slave that has dhcp configured, bring up the
            vrf master now. This is needed because vrf has special handling
            in dhclient hook which requires the vrf master to be present """
        if not self._is_dhcp_slave(ifaceobj):
            return False
        vrf_master = ifaceobj.upperifaces[0]
        if not vrf_master:
            self.logger.warn('%s: vrf master not found' %ifacename)
            return
        if os.path.exists('/sys/class/net/%s' %vrf_master):
            self.logger.info('%s: vrf master %s exists returning'
                             %(ifacename, vrf_master))
            return
        vrf_master_objs = ifaceobj_getfunc(vrf_master)
        if not vrf_master_objs:
            self.logger.warn('%s: vrf master ifaceobj not found' %ifacename)
            return
        self.logger.info('%s: bringing up vrf master %s'
                         %(ifacename, vrf_master))
        for mobj in vrf_master_objs:
            vrf_table = mobj.get_attr_value_first('vrf-table')
            if vrf_table:
                if vrf_table == 'auto':
                    vrf_table = self._get_avail_vrf_table_id()
                    if not vrf_table:
                        self.log_error('%s: unable to get an auto table id'
                                       %mobj.name)
                    self.logger.info('%s: table id auto: selected table id %s\n'
                                     %(mobj.name, vrf_table))
                self._up_vrf_dev(mobj, vrf_table, False)
                break
        self._down_dhcp_slave(ifaceobj)
        self.ipcmd.link_set(ifacename, 'master', vrfname)
        return

    def _down_dhcp_slave(self, ifaceobj):
        try:
            self.dhclientcmd.release(ifaceobj.name)
        except:
            # ignore any dhclient release errors
            pass

    def _up_vrf_slave(self, ifacename, vrfname, ifaceobj=None,
                      ifaceobj_getfunc=None, vrf_exists=False):
        try:
            if vrf_exists or self.ipcmd.link_exists(vrfname):
                upper = self.ipcmd.link_get_upper(ifacename)
                if not upper or upper != vrfname:
                    if ifaceobj and self._is_dhcp_slave(ifaceobj):
                        self._down_dhcp_slave(ifaceobj)
                    self.ipcmd.link_set(ifacename, 'master', vrfname)
            elif ifaceobj:
                self._handle_dhcp_slaves(ifacename, vrfname, ifaceobj,
                                         ifaceobj_getfunc)
        except Exception, e:
            self.log_error('%s: %s' %(ifacename, str(e)))

    def _del_vrf_rules(self, vrf_dev_name, vrf_table):
        pref = 200
        ip_rule_out_format = '%s: from all %s %s lookup %s'
        ip_rule_cmd = 'ip %s rule del pref %s %s %s table %s' 

        rule = ip_rule_out_format %(pref, 'oif', vrf_dev_name, vrf_dev_name)
        if rule in self.ip_rule_cache:
            rule_cmd = ip_rule_cmd %('', pref, 'oif', vrf_dev_name, vrf_table)
            self.exec_command(rule_cmd)

        rule = ip_rule_out_format %(pref, 'iif', vrf_dev_name, vrf_dev_name)
        if rule in self.ip_rule_cache:
            rule_cmd = ip_rule_cmd %('', pref, 'iif', vrf_dev_name, vrf_table)
            self.exec_command(rule_cmd)

        rule = ip_rule_out_format %(pref, 'oif', vrf_dev_name, vrf_dev_name)
        if rule in self.ip6_rule_cache:
            rule_cmd = ip_rule_cmd %('-6', pref, 'oif', vrf_dev_name,
                                     vrf_table)
            self.exec_command(rule_cmd)

        rule = ip_rule_out_format %(pref, 'iif', vrf_dev_name, vrf_dev_name)
        if rule in self.ip6_rule_cache:
            rule_cmd = ip_rule_cmd %('-6', pref, 'iif', vrf_dev_name,
                                     vrf_table)
            self.exec_command(rule_cmd)

    def _add_vrf_rules(self, vrf_dev_name, vrf_table):
        pref = 200
        ip_rule_out_format = '%s: from all %s %s lookup %s'
        ip_rule_cmd = 'ip %s rule add pref %s %s %s table %s' 
        if self.vrf_fix_local_table:
            self.vrf_fix_local_table = False
            rule = '0: from all lookup local'
            if rule in self.ip_rule_cache:
                try:
                    self.exec_command('ip rule del pref 0')
                    self.exec_command('ip rule add pref 32765 table local')
                except Exception, e:
                    self.logger.info('%s' %str(e))
                    pass
            if rule in self.ip6_rule_cache:
                try:
                    self.exec_command('ip -6 rule del pref 0')
                    self.exec_command('ip -6 rule add pref 32765 table local')
                except Exception, e:
                    self.logger.info('%s' %str(e))
                    pass

        #Example ip rule
        #200: from all oif blue lookup blue
        #200: from all iif blue lookup blue

        rule = ip_rule_out_format %(pref, 'oif', vrf_dev_name, vrf_dev_name)
        if rule not in self.ip_rule_cache:
            rule_cmd = ip_rule_cmd %('', pref, 'oif', vrf_dev_name, vrf_table)
            self.exec_command(rule_cmd)

        rule = ip_rule_out_format %(pref, 'iif', vrf_dev_name, vrf_dev_name)
        if rule not in self.ip_rule_cache:
            rule_cmd = ip_rule_cmd %('', pref, 'iif', vrf_dev_name, vrf_table)
            self.exec_command(rule_cmd)

        rule = ip_rule_out_format %(pref, 'oif', vrf_dev_name, vrf_dev_name)
        if rule not in self.ip6_rule_cache:
            rule_cmd = ip_rule_cmd %('-6', pref, 'oif', vrf_dev_name, vrf_table)
            self.exec_command(rule_cmd)

        rule = ip_rule_out_format %(pref, 'iif', vrf_dev_name, vrf_dev_name)
        if rule not in self.ip6_rule_cache:
            rule_cmd = ip_rule_cmd %('-6', pref, 'iif', vrf_dev_name,
                                     vrf_table)
            self.exec_command(rule_cmd)

    def _add_vrf_slaves(self, ifaceobj, ifaceobj_getfunc=None):
        running_slaves = self.ipcmd.link_get_lowers(ifaceobj.name)
        config_slaves = ifaceobj.lowerifaces
        if not config_slaves and not running_slaves:
            return

        if not config_slaves: config_slaves = []
        if not running_slaves: running_slaves = []
        add_slaves = set(config_slaves).difference(set(running_slaves))
        del_slaves = set(running_slaves).difference(set(config_slaves))
        if add_slaves:
            for s in add_slaves:
                try:
                    sobj = None
                    if ifaceobj_getfunc:
                        sobj = ifaceobj_getfunc(s)
                    self._up_vrf_slave(s, ifaceobj.name,
                                       sobj[0] if sobj else None,
                                       ifaceobj_getfunc, True)
                except Exception, e:
                    self.logger.info('%s: %s' %(ifaceobj.name, str(e)))

        if del_slaves:
            for s in del_slaves:
                try:
                    if ifaceobj_getfunc:
                        sobj = ifaceobj_getfunc(s)
                        # if dhcp slave, release the dhcp lease
                        if sobj and self._is_dhcp_slave(sobj[0]):
                            self._down_dhcp_slave(sobj[0])
                    self._down_vrf_slave(s, ifaceobj.name)
                except Exception, e:
                    self.logger.info('%s: %s' %(ifaceobj.name, str(e)))

        if ifaceobj.link_type == ifaceLinkType.LINK_MASTER:
            for s in config_slaves:
                try:
                    rtnetlink_api.rtnl_api.link_set(s, "up")
                except Exception, e:
                    self.logger.debug('%s: %s: link set up (%s)'
                                      %(ifaceobj.name, s, str(e)))
                    pass

    def _create_cgroup(self, ifaceobj):
        if not self.vrf_cgroup_create:
            return
        try:
            if not os.path.exists('/sys/fs/cgroup/l3mdev/%s' %ifaceobj.name):
                self.exec_command('/usr/bin/cgcreate -g l3mdev:%s' %ifaceobj.name)
        except Exception, e:
            self.log_error('%s: cgroup create failed (%s)\n'
                           %(ifaceobj.name, str(e)), ifaceobj)
        try:
            self.exec_command('/usr/bin/cgset -r l3mdev.master-device=%s %s'
                              %(ifaceobj.name, ifaceobj.name))
        except Exception, e:
            self.log_warn('%s: cgset failed (%s)\n'
                          %(ifaceobj.name, str(e)), ifaceobj)

    def _set_vrf_dev_processed_flag(self, ifaceobj):
        ifaceobj.module_flags[self.name] = \
                             ifaceobj.module_flags.setdefault(self.name, 0) | \
                                        vrfPrivFlags.PROCESSED

    def _check_vrf_dev_processed_flag(self, ifaceobj):
        if (ifaceobj.module_flags.get(self.name, 0x0) & vrfPrivFlags.PROCESSED):
            return True
        return False

    def _create_vrf_dev(self, ifaceobj, vrf_table):
        if not self.ipcmd.link_exists(ifaceobj.name):
            if vrf_table == 'auto':
                vrf_table = self._get_avail_vrf_table_id()
                if not vrf_table:
                    self.log_error('%s: unable to get an auto table id'
                                   %ifaceobj.name)
                self.logger.info('%s: table id auto: selected table id %s\n'
                                 %(ifaceobj.name, vrf_table))

            if not vrf_table.isdigit():
                self.log_error('%s: vrf-table must be an integer or \'auto\''
                               %(ifaceobj.name), ifaceobj)

            # XXX: If we decide to not allow vrf id usages out of
            # the reserved ifupdown range, then uncomment this code.
            #else:
            #    if (int(vrf_table) < self.vrf_table_id_start or
            #        int(vrf_table) > self.vrf_table_id_end):
            #        self.log_error('%s: vrf table id %s out of reserved range [%d,%d]'
            #                       %(ifaceobj.name, vrf_table,
            #                         self.vrf_table_id_start,
            #                         self.vrf_table_id_end))
            try:
                self.ipcmd.link_create(ifaceobj.name, 'vrf',
                                       {'table' : '%s' %vrf_table})
            except Exception, e:
                self.log_error('%s: create failed (%s)\n'
                               %(ifaceobj.name, str(e)))
        else:
            if vrf_table == 'auto':
                vrf_table = self._get_iproute2_vrf_table(ifaceobj.name)
                if not vrf_table:
                    self.log_error('%s: unable to get vrf table id'
                                   %ifaceobj.name)

            # if the device exists, check if table id is same
            vrfdev_attrs = self.ipcmd.link_get_linkinfo_attrs(ifaceobj.name)
            if vrfdev_attrs:
                running_table = vrfdev_attrs.get('table', None)
                if vrf_table != running_table:
                    self.log_error('%s: cannot change vrf table id,running table id %s is different from config id %s' %(ifaceobj.name,
                                         running_table, vrf_table))
        if vrf_table != 'auto':
            self._iproute2_vrf_table_entry_add(ifaceobj.name, vrf_table)

        return vrf_table

    def _add_vrf_default_route(self, ifaceobj,  vrf_table):
        vrf_default_route = ifaceobj.get_attr_value_first('vrf-default-route')
        if not vrf_default_route:
            vrf_default_route = policymanager.policymanager_api.get_attr_default(
                                    module_name=self.__class__.__name__,
                                    attr='vrf-default-route')
        if not vrf_default_route:
            return
        if str(vrf_default_route).lower() == "yes":
            try:
                self.exec_command('ip route add table %s unreachable default'
                                  ' metric %d' %(vrf_table, 240))
            except OSError, e:
                if e.errno != 17:
                    raise
                pass

            try:
                self.exec_command('ip -6 route add table %s unreachable '
                                  'default metric %d' %(vrf_table, 240))
            except OSError, e:
                if e.errno != 17:
                    raise
                pass

    def _up_vrf_dev(self, ifaceobj, vrf_table, add_slaves=True,
                    ifaceobj_getfunc=None):

        # if vrf dev is already processed return. This can happen
        # if we had a dhcp slave. See self._handle_dhcp_slaves
        if self._check_vrf_dev_processed_flag(ifaceobj):
            return True

        vrf_table = self._create_vrf_dev(ifaceobj, vrf_table)
        try:
            self._add_vrf_rules(ifaceobj.name, vrf_table)
            self._create_cgroup(ifaceobj)
            if add_slaves:
                self._add_vrf_slaves(ifaceobj, ifaceobj_getfunc)
            self._add_vrf_default_route(ifaceobj, vrf_table)
            self._set_vrf_dev_processed_flag(ifaceobj)
        except Exception, e:
            self.log_error('%s: %s' %(ifaceobj.name, str(e)))

    def _up(self, ifaceobj, ifaceobj_getfunc=None):
        try:
            vrf_table = ifaceobj.get_attr_value_first('vrf-table')
            if vrf_table:
                # This is a vrf device
                if self.vrf_count == self.vrf_max_count:
                    self.log_error('%s: max vrf count %d hit...not '
                                   'creating vrf' %(ifaceobj.name,
                                                    self.vrf_count))
                self._up_vrf_dev(ifaceobj, vrf_table, True, ifaceobj_getfunc)
            else:
                vrf = ifaceobj.get_attr_value_first('vrf')
                if vrf:
                    # This is a vrf slave
                    self._up_vrf_slave(ifaceobj.name, vrf, ifaceobj,
                                       ifaceobj_getfunc)
        except Exception, e:
            self.log_error(str(e))

    def _delete_cgroup(self, ifaceobj):
        try:
            if os.path.exists('/sys/fs/cgroup/l3mdev/%s' %ifaceobj.name):
                self.exec_command('/usr/bin/cgdelete -g l3mdev:%s' %ifaceobj.name)
        except Exception, e:
            self.log_info('%s: cgroup delete failed (%s)\n'
                          %(ifaceobj.name, str(e)), ifaceobj)

    def _down_vrf_dev(self, ifaceobj, vrf_table, ifaceobj_getfunc=None):
        if vrf_table == 'auto':
            vrf_table = self._get_iproute2_vrf_table(ifaceobj.name)
        try:
            running_slaves = self.ipcmd.link_get_lowers(ifaceobj.name)
            if running_slaves:
                for s in running_slaves:
                    if ifaceobj_getfunc:
                        sobj = ifaceobj_getfunc(s)
                        # if dhcp slave, release the dhcp lease
                        if sobj and self._is_dhcp_slave(sobj[0]):
                            self._down_dhcp_slave(sobj[0])
            self.ipcmd.link_delete(ifaceobj.name)
        except Exception, e:
            self.logger.info('%s: %s' %(ifaceobj.name, str(e)))
            pass

        try:
            self._iproute2_vrf_table_entry_del(vrf_table)
            self._delete_cgroup(ifaceobj)
        except Exception, e:
            self.logger.info('%s: %s' %(ifaceobj.name, str(e)))
            pass

        try:
            self._del_vrf_rules(ifaceobj.name, vrf_table)
        except Exception, e:
            self.logger.info('%s: %s' %(ifaceobj.name, str(e)))
            pass

    def _down_vrf_slave(self, ifacename, vrf):
        try:
            self.ipcmd.link_set(ifacename, 'nomaster')
        except Exception, e:
            self.logger.warn('%s: %s' %(ifacename, str(e)))

    def _down(self, ifaceobj, ifaceobj_getfunc=None):
        try:
            vrf_table = ifaceobj.get_attr_value_first('vrf-table')
            if vrf_table:
                self._down_vrf_dev(ifaceobj, vrf_table, ifaceobj_getfunc)
            else:
                vrf = ifaceobj.get_attr_value_first('vrf')
                if vrf:
                    self._down_vrf_slave(ifaceobj.name, vrf)
        except Exception, e:
            self.log_warn(str(e))

    def _query_check_vrf_slave(self, ifaceobj, ifaceobjcurr, vrf):
        try:
            master = self.ipcmd.link_get_master(ifaceobj.name)
            if not master or master != vrf:
                ifaceobjcurr.update_config_with_status('vrf', master, 1)
            else:
                ifaceobjcurr.update_config_with_status('vrf', master, 0)
        except Exception, e:
            self.log_warn(str(e))

    def _query_check_vrf_dev(self, ifaceobj, ifaceobjcurr, vrf_table):
        try:
            if not self.ipcmd.link_exists(ifaceobj.name):
                self.logger.info('%s: vrf: does not exist' %(ifaceobj.name))
                return
            if vrf_table == 'auto':
                config_table = self._get_iproute2_vrf_table(ifaceobj.name)
            else:
                config_table = vrf_table
            vrfdev_attrs = self.ipcmd.link_get_linkinfo_attrs(ifaceobj.name)
            if not vrfdev_attrs:
                ifaceobjcurr.update_config_with_status('vrf-table', 'None', 1)
                return
            running_table = vrfdev_attrs.get('table')
            if not running_table:
                ifaceobjcurr.update_config_with_status('vrf-table', 'None', 1)
                return
            if config_table != running_table:
                ifaceobjcurr.update_config_with_status('vrf-table',
                                                       running_table, 1)
            else:
                ifaceobjcurr.update_config_with_status('vrf-table',
                                                       running_table, 0)
        except Exception, e:
            self.log_warn(str(e))

    def _query_check(self, ifaceobj, ifaceobjcurr):
        try:
            vrf_table = ifaceobj.get_attr_value_first('vrf-table')
            if vrf_table:
                self._query_check_vrf_dev(ifaceobj, ifaceobjcurr, vrf_table)
            else:
                vrf = ifaceobj.get_attr_value_first('vrf')
                if vrf:
                    self._query_check_vrf_slave(ifaceobj, ifaceobjcurr, vrf)
        except Exception, e:
            self.log_warn(str(e))

    def _query_running(self, ifaceobjrunning, ifaceobj_getfunc=None):
        try:
            kind = self.ipcmd.link_get_kind(ifaceobjrunning.name)
            if kind == 'vrf':
                vrfdev_attrs = self.ipcmd.link_get_linkinfo_attrs(ifaceobjrunning.name)
                if vrfdev_attrs:
                    running_table = vrfdev_attrs.get('table')
                    if running_table:
                        ifaceobjrunning.update_config('vrf-table',
                                                      running_table)
            elif kind == 'vrf_slave':
                vrf = self.ipcmd.link_get_master(ifaceobjrunning.name)
                if vrf:
                    ifaceobjrunning.update_config('vrf', vrf)
        except Exception, e:
            self.log_warn(str(e))

    _run_ops = {'pre-up' : _up,
               'post-down' : _down,
               'query-running' : _query_running,
               'query-checkcurr' : _query_check}

    def get_ops(self):
        """ returns list of ops supported by this module """
        return self._run_ops.keys()

    def _init_command_handlers(self):
        flags = self.get_flags()
        if not self.ipcmd:
            self.ipcmd = iproute2(**flags)
        if not self.bondcmd:
            self.bondcmd = bondutil(**flags)
        if not self.dhclientcmd:
            self.dhclientcmd = dhclient(**flags)

    def run(self, ifaceobj, operation, query_ifaceobj=None,
            ifaceobj_getfunc=None, **extra_args):
        """ run bond configuration on the interface object passed as argument

        Args:
            **ifaceobj** (object): iface object

            **operation** (str): any of 'pre-up', 'post-down', 'query-checkcurr',
                'query-running'

        Kwargs:
            **query_ifaceobj** (object): query check ifaceobject. This is only
                valid when op is 'query-checkcurr'. It is an object same as
                ifaceobj, but contains running attribute values and its config
                status. The modules can use it to return queried running state
                of interfaces. status is success if the running state is same
                as user required state in ifaceobj. error otherwise.
        """
        op_handler = self._run_ops.get(operation)
        if not op_handler:
            return
        self._init_command_handlers()
        if operation == 'query-checkcurr':
            op_handler(self, ifaceobj, query_ifaceobj)
        else:
            op_handler(self, ifaceobj, ifaceobj_getfunc=ifaceobj_getfunc)
