from dataclasses import dataclass
import ipaddress
import json
import os
import re
import sys
from threading import Timer

import docker
from flask import Flask

@dataclass
class Reservation:
    vmid: int
    interface: int
    mac: str
    ip: ipaddress.IPv4Address

    def json(self):
        return {
            'mac': self.mac,
            'ip': str(self.ip),
            'vmid': self.vmid,
            'interface': self.interface
        }

class InterfaceReservations:
    interface: str
    vlan: int
    subnet: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address | None

    reservations: list[Reservation]

    rebuild: bool = True
    status: str = 'Not started'

    def build_config(self, reservations=None) -> str:
        conf = {
            'Dhcp4': {
                'interfaces-config': {
                    'interfaces': ['eth0']
                },
            },
            'subnet4': [
                {
                    'pools': [{
                        'pool': f'{str(self.subnet[2])} - {str(self.subnet[3])}'
                    }],
                    'id': 1,
                    'subnet': str(self.subnet),
                    'reservations': [
                        {
                            'hw-address': r['mac'],
                            'ip-address': str(r['ip'])
                        }
                        for r in self.reservations
                    ]
                }
            ]
        }

        if self.gateway:
            conf['Dhcp4']['option-data'] = [{
                'name': 'routers',
                'data': str(self.gateway)
            }]

        return json.dumps(conf)

    def build_leases(self) -> str:
        return f"""address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state,user_context,pool_id
{str(self.subnet[2])},00:00:00:00:00:00,00:00:00:00:00:00:00,31536000,32503611600000,1,0,0,unknown,0,,0
{str(self.subnet[3])},00:00:00:00:00:00,00:00:00:00:00:00:00,31536000,32503611600000,1,0,0,unknown,0,,0
        """

    def json(self) -> dict:
        return {
            'interface': self.interface,
            'vlan': self.vlan,
            'subnet_id': str(self.subnet.network_address),
            'subnet_mask': self.subnet.prefixlen,
            'gateway': str(self.gateway),
            'status': self.status,

            'reservations': [r.json() for r in self.reservations]
        }

server = Flask(__name__)

client = docker.from_env()

raw_query = []
interfaces: list[InterfaceReservations] = []
errors: list[str] = []
crash: Exception | None = None

@server.route("/stats_raw")
def get_stats_raw():
    return [
        {
            'bridge': q['bridge'],
            'vlan': q['vlan'],
            'subnet': str(q['subnet']),
            'gateway': str(q['gateway']),
            'reservations': [
                {
                    'vmid': r['vmid'],
                    'mac': r['mac'],
                    'ip': str(r['ip']),
                    'interface': r['interface']
                }
                for r in q['reservations']
            ]
        }
        for q in raw_query
    ]

@server.route("/stats")
def get_stats():
    return {
        'errors': errors,
        'interfaces': [i.json() for i in interfaces],
        'crash': crash.message if crash else None
    }

@server.route("/")
def get_webpage():
    return server.send_static_file("index.html")

class RepeatTimer(Timer):
    def run(self):
        while not self.finished.wait(self.interval):
            try:
                self.function(*self.args, **self.kwargs)
                crash = None
            except Exception as e:
                crash = e

def parse_kv(inp: str) -> dict[str, str]:
    return dict(
        (key, value) for [key, value] in
        (opt.split('=', 1) for opt in inp.split(','))
    )

def query_reservations():
    errors.clear()

    vms = os.listdir('/etc/pve/local/qemu-server')

    results = {}

    for vm in vms:
        try:
            vm_id = int(vm.split('.conf')[0])
        except:
            continue

        with open(f'/etc/pve/local/qemu-server/{vm_id}.conf', 'r') as f:
            lines = [
                line.strip().split(': ', 1)
                for line in f.readlines()
                if ': ' in line and not line.startswith('#')
            ]
            options = dict((key, value) for [key, value] in lines)

            for [key, value] in lines:
                if key.startswith('ipconfig'):
                    net_id = int(key[len('ipconfig'):])
                    net_conf = parse_kv(options[f'net{net_id}'])

                    if net_conf.get('firewall', '0') == '1':
                        interface = f'fwbr{vm_id}i{net_id}'
                        tag = 0
                    elif 'tag' in net_conf:
                        tag = int(net_conf['tag'])
                        interface = f"{net_conf['bridge']}.{tag}"
                    else:
                        interface = net_conf['bridge']
                        tag = 0

                    ip_config = parse_kv(value)

                    address = ipaddress.ip_interface(ip_config['ip'])
                    gateway = ipaddress.ip_address(ip_config['gw']) if 'gw' in ip_config else None

                    results[interface] = results.get(
                        interface,
                        {
                            'bridge': interface,
                            'vlan': tag,
                            'subnet': address.network,
                            'gateway': gateway,
                            'reservations': []
                        }
                    )
                    if_opts = results[interface]

                    if not address.ip in if_opts['subnet']:
                        errors.append(
                            f'VM ID {vm_id} network interface {net_id} has an IP assigned of {str(address.ip)}, which does not reside in {if_opts["subnet"]} previously defined as used for network {interface}'
                        )

                    if if_opts['gateway'] != gateway:
                        errors.append(
                            f'VM ID {vm_id} network interface {net_id} has an gateway with a value of {str(gateway)}, which does not match the gateway for {interface} of {if_opts["gateway"]}'
                        )

                    if_opts['reservations'].append({
                        'vmid': vm_id,
                        'interface': net_id,
                        'mac': next(
                            mac for mac in net_conf.values()
                            if re.match(r"([A-F0-9]{2}:){5}[A-F0-9]{2}", mac)
                        ),
                        'ip': address.ip
                    })

    return results

def update_reservations():
    raw_query.clear()
    new_reservations = query_reservations()
    raw_query.extend(new_reservations.values())

    for new_res in new_reservations.values():
        res_list = [Reservation(**r) for r in new_res['reservations']]

        for interface in interfaces:
            if interface.interface == new_res['bridge']:
                old_config = interface.build_config()
                new_config = interface.build_config(res_list)

                if old_config != new_config:
                    interface.reservations = res_list
                    interface.rebuild = True
                    interface.status = 'Pending rebuild'

                break
        else:
            interface = InterfaceReservations()
            interface.interface = new_res['bridge']
            interface.vlan = new_res['vlan']
            interface.subnet = new_res['subnet']
            interface.gateway = new_res['gateway']
            interface.reservations = res_list
            interfaces.append(interface)

    for interface in interfaces:
        for new_res in new_reservations.values():
            if interface.interface == new_res['bridge']:
                break
        else:
            interface.status = 'No longer needed'

    #for res in new_reservations.values():
    #    print(f'Interface {res["bridge"]} ({res["subnet"]})')
    #    for r in res['reservations']:
    #        print(f'\t{r["vmid"]}: {r["ip"]} ({r["mac"]})')

    #print(json.dumps(errors, indent=4))

timer = RepeatTimer(
    int(os.environ.get('VM_CHECK_POLL', '30')),
    update_reservations
)

if __name__ == "__main__":
    containers = [
        c for c in client.containers.list()
        if 'co.riouxs.keaproxmox.interface' in c.labels
    ]

    if len(containers) != 0:
        print('Stopping extra containers:')

    for container in containers:
        print(f' * Stopping {container.name}')
        container.stop()

    print('Pruning networks')
    client.networks.prune()
    print('Networks pruned')

    print('Running background thread to spawn DHCP servers')
    timer.start()

    print('Running server')
    server.run(host='0.0.0.0')
