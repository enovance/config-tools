#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2015 eNovance SAS <licensing@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import os.path
import random
import re
import string
import subprocess
import sys
import tempfile
import uuid

import ipaddress
import jinja2
import libvirt
import six
import yaml

# from pprint import pprint


def random_mac():
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255))


def canical_size(size):
    """Convert size to GB or MB

    Convert GiB to MB or return the original string.

    """
    gi = re.search('^(\d+)Gi', size)
    if gi:
        new_size = "%i" % (int(gi.group(1)) * 1000 ** 3)
    else:
        new_size = size
    return new_size


def get_conf(argv=sys.argv):
    parser = argparse.ArgumentParser(
        description='Deploy a virtual infrastructure.')
    parser.add_argument('--replace', action='store_true',
                        help='existing resources will be recreated.')
    parser.add_argument('input_file', type=str,
                        help='the input file.')
    parser.add_argument('target_host', type=str,
                        help='the libvirt server.')
    parser.add_argument('--pub-key-file', type=str,
                        default=os.path.expanduser(
                            '~/.ssh/id_rsa.pub'),
                        help='SSH public key file.')
    conf = parser.parse_args(argv)
    return conf


class Host(object):
    host_template_string = """
<domain type='kvm'>
  <name>{{ hostname }}</name>
  <uuid>{{ uuid }}</uuid>
  <memory unit='KiB'>{{ memory }}</memory>
  <currentmemory unit='KiB'>{{ memory }}</currentmemory>
  <vcpu>{{ ncpus }}</vcpu>
  <os>
    <smbios mode='sysinfo'/>
    <type arch='x86_64' machine='pc'>hvm</type>
    <bios useserial='yes' rebootTimeout='2'/>
  </os>
  <sysinfo type='smbios'>
    <bios>
      <entry name='vendor'>eNovance</entry>
    </bios>
    <system>
      <entry name='manufacturer'>QEMU</entry>
      <entry name='product'>virtualizor</entry>
      <entry name='version'>1.0</entry>
    </system>
  </sysinfo>
  <features>
    <acpi/>
    <apic/>
    <pae/>
  </features>
  <clock offset='utc'/>
  <on_poweroff>restart</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
{% for disk in disks %}
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{{ disk.path }}'/>
      <target dev='{{ disk.name }}' bus='virtio'/>
{% if disk.boot_order is defined %}
      <boot order='{{ disk.boot_order }}'/>
{% endif %}
    </disk>
{% endfor %}
{% if is_install_server is defined %}
    <disk type='file' device='disk'>
      <driver name='qemu' type='raw'/>
      <source file='/var/lib/libvirt/images/cloud-init.iso'/>
      <target dev='vdz' bus='virtio'/>
    </disk>
{% endif %}
{% for nic in nics %}
{% if nic.network_name is defined %}
    <interface type='network'>
      <mac address='{{ nic.mac }}'/>
      <source network='{{ nic.network_name }}'/>
      <model type='virtio'/>
{% if nic.boot_order is defined %}
      <boot order='{{ nic.boot_order }}'/>
{% endif %}
    </interface>
{% endif %}
{% endfor %}
    <serial type='pty'>
      <target port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <input type='mouse' bus='ps2'/>
    <graphics type='vnc' port='-1' autoport='yes'/>
    <video>
      <model type='cirrus' vram='9216' heads='1'/>
    </video>
  </devices>
</domain>
    """
    host_libvirt_image_dir = "/var/lib/libvirt/images"
    user_data_template_string = """#cloud-config
users:
 - default
 - name: jenkins
   ssh-authorized-keys:
{% for ssh_key in ssh_keys %}   - {{ ssh_key|trim }}
{% endfor %}
 - name: root
   ssh-authorized-keys:
{% for ssh_key in ssh_keys %}   - {{ ssh_key|trim }}
{% endfor %}

write_files:
  - path: /etc/resolv.conf
    content: |
      nameserver 8.8.8.8
      options rotate timeout:1
  - path: /etc/sudoers.d/jenkins-cloud-init
    permissions: 0440
    content: |
      Defaults:jenkins !requiretty
      jenkins ALL=(ALL) NOPASSWD:ALL
  - path: /etc/sysconfig/network-scripts/ifcfg-eth0
    content: |
      DEVICE=eth0
      BOOTPROTO=none
      ONBOOT=yes
      IPADDR={{ ip }}
      NETWORK={{ network }}
      NETMASK={{ netmask }}
      GATEWAY={{ gateway }}
  - path: /etc/sysconfig/network
    content: |
      NETWORKING=yes
      NOZEROCONF=no
      GATEWAY=10.10.0.254
      HOSTNAME={{ hostname }}
      GATEWAY={{ gateway }}

runcmd:
 - /bin/rm -f /etc/yum.repos.d/*.repo

"""
    meta_data_template_string = """
instance-id: id-install-server
local-hostname: {{ hostname }}

"""

    def __init__(self, conf, definition, install_server_info):
        self.conf = conf
        self.hostname = definition['hostname']
        self.meta = {'hostname': definition['hostname'],
                     'uuid': str(uuid.uuid1()),
                     'memory': 4194304,
                     'ncpus': 1,
                     'cpus': [], 'disks': [], 'nics': []}

        for k in ('uuid', 'serial', 'product_name',
                  'memory', 'ncpus', 'is_install_server'):
            if k not in definition:
                continue
            self.meta[k] = definition[k]

        if definition['profile'] == 'install-server':
            print("  This is the install-server")
            self.meta['is_install_server'] = True
            definition['disks'] = [
                {'name': 'vda',
                 'size': '30G',
                 'clone_from':
                     '/var/lib/libvirt/images/install-server-%s.img.qcow2' %
                         install_server_info['version']}
            ]
            definition['nics'][0].update({'mac': install_server_info['mac']})
            self.prepare_cloud_init(
                ip=install_server_info['ip'],
                network=install_server_info['network'],
                netmask=install_server_info['netmask'],
                gateway=install_server_info['gateway'])

        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.template = env.from_string(Host.host_template_string)

        definition['nics'][0]['boot_order'] = 1
        definition['disks'][0]['boot_order'] = 2

        self.register_disks(definition)
        self.register_nics(definition)

    def _push(self, source, dest):
        subprocess.call(['scp', '-r', source,
                         'root@%s' % self.conf.target_host + ':' + dest])

    def _call(self, *kargs):
        subprocess.call(['ssh', 'root@%s' % self.conf.target_host] +
                        list(kargs))

    def prepare_cloud_init(self, ip, network, netmask, gateway):

        ssh_key_file = self.conf.pub_key_file
        meta = {
            'ssh_keys': open(ssh_key_file).readlines(),
            'hostname': self.hostname,
            'ip': ip,
            'network': network,
            'netmask': netmask,
            'gateway': gateway
        }
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        contents = {
            'user-data': env.from_string(Host.user_data_template_string),
            'meta-data': env.from_string(Host.meta_data_template_string)}
        # TODO(Gonéri): use mktemp
        self._call("mkdir", "-p", "/tmp/mydata")
        for name in sorted(contents):
            fd = tempfile.NamedTemporaryFile()
            fd.write(contents[name].render(meta))
            fd.seek(0)
            fd.flush()
            self._push(fd.name, '/tmp/mydata/' + name)

        self._call('genisoimage', '-quiet', '-output',
                   Host.host_libvirt_image_dir + '/cloud-init.iso',
                   '-volid', 'cidata', '-joliet', '-rock',
                   '/tmp/mydata/user-data', '/tmp/mydata/meta-data')

    def register_disks(self, definition):
        cpt = 0
        for info in definition['disks']:
            filename = "%s-%03d.qcow2" % (self.hostname, cpt)
            if 'clone_from' in info:
                self._call('qemu-img', 'create', '-f', 'qcow2',
                           '-b', info['clone_from'],
                           Host.host_libvirt_image_dir +
                           '/' + filename, info['size'])
                self._call('qemu-img', 'resize', '-q',
                           Host.host_libvirt_image_dir + '/' + filename,
                           canical_size(info['size']))
            else:
                self._call('qemu-img', 'create', '-q', '-f', 'qcow2',
                           Host.host_libvirt_image_dir + '/' + filename,
                           canical_size(info['size']))

            info.update({
                'name': 'vd' + string.ascii_lowercase[cpt],
                'path': Host.host_libvirt_image_dir + '/' + filename})
            self.meta['disks'].append(info)
            cpt += 1

    def register_nics(self, definition):
        i = 0
        for info in definition['nics']:
            info.update({
                'mac': info.get('mac', random_mac()),
                'name': info.get('name', 'noname%i' % i),
                'network_name': 'sps_default'
            })
            self.meta['nics'].append(info)
            i += 1

    def dump_libvirt_xml(self):
        return self.template.render(self.meta)


class Network(object):
    network_template_string = """
<network>
  <name>{{ name }}</name>
  <uuid>{{ uuid }}</uuid>
  <bridge name='{{ bridge_name }}' stp='on' delay='0'/>
  <mac address='{{ mac }}'/>
{% if dhcp is defined %}
  <forward mode='nat'>
    <nat>
      <port start='1024' end='65535'/>
    </nat>
  </forward>
  <ip address='{{ dhcp.address }}' netmask='{{ dhcp.netmask }}'>
    <dhcp>
{% for host in dhcp.hosts %}
      <range start='{{ host.ip }}' end='{{ host.ip }}' />
      <host mac='{{ host.mac }}' name='{{ host.name }}' ip='{{ host.ip }}'/>
{% endfor %}
    </dhcp>
  </ip>
{% endif %}
</network>
    """

    def __init__(self, name, definition):
        self.name = name
        self.meta = {
            'name': name,
            'uuid': str(uuid.uuid1()),
            'mac': random_mac(),
            'bridge_name': 'virbr%d' % random.randrange(0, 0xffffffff)}

        for k in ('uuid', 'mac', 'ips', 'dhcp'):
            if k not in definition:
                continue
            self.meta[k] = definition[k]

        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        self.template = env.from_string(Network.network_template_string)

    def dump_libvirt_xml(self):
        return self.template.render(self.meta)


def get_install_server_info(conn, hosts_definition):
    for hostname, definition in six.iteritems(hosts_definition['hosts']):
        if definition.get('profile', '') == 'install-server':
            break

    print("install-server (%s)" % (hostname))
    admin_nic_info = definition['nics'][0]
    network = ipaddress.ip_network(
        unicode(
            admin_nic_info['network'] + '/' + admin_nic_info['netmask']))
    admin_nic_info = definition['nics'][0]
    return {
        'mac': admin_nic_info.get('mac', random_mac()),
        'hostname': hostname,
        'ip': admin_nic_info['ip'],
        'gateway': str(network.network_address + 1),
        'netmask': str(network.netmask),
        'network': str(network.network_address),
        'version': hosts_definition.get('version', 'RH7.0-I.1.2.1'),
    }


def create_network(conn, netname, install_server_info):
    net_definition = {'dhcp': {
        'address': install_server_info['gateway'],
        'netmask': install_server_info['netmask'],
        'hosts': [{'mac': install_server_info['mac'],
                   'name': install_server_info['hostname'],
                   'ip': install_server_info['ip']}]}}
    network = Network(netname, net_definition)
    conn.networkCreateXML(network.dump_libvirt_xml())


def main(argv=sys.argv[1:]):
    conf = get_conf(argv)

    netname = 'sps_default'
    hosts_definition = yaml.load(open(conf.input_file, 'r'))
    conn = libvirt.open('qemu+ssh://root@%s/system' % conf.target_host)
    install_server_info = get_install_server_info(conn, hosts_definition)

    existing_networks = ([n.name() for n in conn.listAllNetworks()])
    if netname in existing_networks:
        if conf.replace:
            conn.networkLookupByName(netname).destroy()
            print("Cleaning network %s." % netname)
            create_network(conn, netname, install_server_info)
    else:
        create_network(conn, netname, install_server_info)

    hosts = hosts_definition['hosts']
    existing_hosts = ([n.name() for n in conn.listAllDomains()])
    for hostname in sorted(hosts):
        definition = hosts[hostname]
        definition['hostname'] = hostname
        if hostname in existing_hosts:
            if conf.replace:
                dom = conn.lookupByName(hostname)
                if dom.info()[0] in [libvirt.VIR_DOMAIN_RUNNING,
                                     libvirt.VIR_DOMAIN_PAUSED]:
                    dom.destroy()
                if dom.info()[0] in [libvirt.VIR_DOMAIN_SHUTOFF]:
                    dom.undefine()
                print("Recreating host %s." % hostname)
            else:
                print("Host %s already exist." % hostname)
                continue
        host = Host(conf, definition, install_server_info)
        conn.defineXML(host.dump_libvirt_xml())
        dom = conn.lookupByName(hostname)
        dom.create()


if __name__ == '__main__':
    main()
