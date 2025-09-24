from dataclasses import dataclass
import ipaddress
import json
import os
import re
import subprocess
import sys
from threading import Thread, Timer

from flask import Flask


@dataclass
class Reservation:
    vmid: int
    interface: int
    mac: str
    ip: ipaddress.IPv4Address
    dns_server: ipaddress.IPv4Address | None
    dns_search: ipaddress.IPv4Address | None

    def json(self):
        return {
            "mac": self.mac,
            "ip": str(self.ip),
            "vmid": self.vmid,
            "interface": self.interface,
            "dns_server": self.dns_server,
            "dns_search": self.dns_search
        }


class InterfaceReservations(Thread):
    interface: str
    if_raw: str
    vlan: int
    subnet: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address | None

    reservations: list[Reservation]
    allocated_reservations: list[Reservation]

    kea_process = None

    rebuild: bool = True
    status: str = "Not started"

    def rebuild_if(self):
        ifr = InterfaceReservations()

        ifr.interface = self.interface
        ifr.if_raw = self.if_raw
        ifr.vlan = self.vlan
        ifr.subnet = self.subnet
        ifr.gateway = self.gateway
        ifr.reservations = self.reservations
        ifr.allocated_reservations = self.allocated_reservations
        ifr.kea_process = self.kea_process
        ifr.rebuild = self.rebuild
        ifr.status = self.status

        return ifr

    def build_config(self, reservations=None) -> str:
        res = reservations if reservations else self.reservations
        conf = {
            "Dhcp4": {
                "interfaces-config": {"interfaces": [f"kn_{self.interface}"]},
                "lease-database": {
                    "type": "memfile",
                    "name": f"/etc/pkci/{self.interface}/leases.csv",
                    "lfc-interval": 0,
                },
                "subnet4": [
                    {
                        "pools": [
                            {
                                "pool": f"{str(self.subnet[1])} - {str(self.subnet[-2])}",
                                "client-class": "cloudinit",
                            }
                        ],
                        "id": 1,
                        "subnet": str(self.subnet),
                        "reservations": [
                            {
                                "hw-address": r.mac,
                                "ip-address": str(r.ip),
                                "client-classes": ["cloudinit"],
                                "option-data": [
                                    opt for opt in [
                                        {
                                            "name": "domain-name-servers",
                                            "data": r.dns_server,
                                            "always-send": True
                                        } if r.dns_server else None,
                                        {
                                            "name": "domain-name",
                                            "data": r.dns_search,
                                            "always-send": True
                                        } if r.dns_search else None
                                    ] if opt is not None
                                ]
                            }
                            for r in res
                        ],
                    }
                ],
            }
        }

        if self.gateway:
            conf["Dhcp4"]["option-data"] = [
                {"name": "routers", "data": str(self.gateway)}
            ]

        return json.dumps(conf)

    def build_leases(self) -> str:
        return "address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state,user_context,pool_id\n"

    def json(self) -> dict:
        return {
            "interface": self.interface,
            "vlan": self.vlan,
            "subnet_id": str(self.subnet.network_address),
            "subnet_mask": self.subnet.prefixlen,
            "gateway": str(self.gateway),
            "status": self.status,
            "reservations": [r.json() for r in self.reservations],
            "allocated_reservations": [r.json() for r in self.allocated_reservations],
        }

    def run(self):
        with open(f"/etc/pkci/{self.interface}/kea-dhcp4.json", "w") as kea_dhcp:
            kea_dhcp.write(self.build_config())

        with open(f"/etc/pkci/{self.interface}/leases.csv", "w") as kea_leases:
            kea_leases.write(self.build_leases())

        self.kea_process = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                f'unshare -m sh -c "mount -t tmpfs kea_run /var/run/kea; KEA_DHCP_DATA_DIR=/etc/pkci/{self.interface} ip netns exec kea_{self.interface} kea-dhcp4 -c /etc/pkci/{self.interface}/kea-dhcp4.json 2>&1 | tee /etc/pkci/{self.interface}/log"',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        while self.kea_process.returncode is None:
            out, err = self.kea_process.communicate(timeout=15)

            if err != "":
                for reservation in self.reservations:
                    for alloced in self.allocated_reservations:
                        if alloced == reservation:
                            break
                    else:
                        if f"lease {str(reservation.ip)} has been allocated" in err:
                            self.allocated_reservations.append(reservation)

        self.status = "Exited (likely error!)"

    def stop(self):
        os.system(f"ip netns del kea_{self.interface}")
        os.system(f"ip link del kh_{self.interface}")

        if self.kea_process:
            self.kea_process.kill()

        if self.is_alive():
            self.join()


server = Flask(__name__)

raw_query = []
interfaces: list[InterfaceReservations] = []
errors: list[str] = []
crash: Exception | None = None


@server.route("/stats_raw")
def get_stats_raw():
    return [
        {
            "bridge": q["bridge"],
            "vlan": q["vlan"],
            "subnet": str(q["subnet"]),
            "gateway": str(q["gateway"]),
            "reservations": [
                {
                    "vmid": r["vmid"],
                    "mac": r["mac"],
                    "ip": str(r["ip"]),
                    "interface": r["interface"],
                    "dns_server": r["dns_server"],
                    "dns_search": r["dns_search"]
                }
                for r in q["reservations"]
            ],
        }
        for q in raw_query
    ]


@server.route("/stats")
def get_stats():
    return {
        "errors": errors,
        "interfaces": [i.json() for i in interfaces],
        "crash": crash.message if crash else None,
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
        (key, value) for [key, value] in (opt.split("=", 1) for opt in inp.split(","))
    )


def query_reservations():
    errors.clear()

    vms = os.listdir("/etc/pve/local/qemu-server")

    results = {}

    for vm in vms:
        try:
            vm_id = int(vm.split(".conf")[0])
        except:
            continue

        try:
            with open(f"/etc/pve/local/qemu-server/{vm_id}.conf", "r") as f:
                lines = [
                    line.strip().split(": ", 1)
                    for line in f.readlines()
                    if ": " in line and len(line.strip().split(": ", 1)) == 2
                ]
                options = dict((key, value) for [key, value] in lines)

                dns_server = None
                dns_search = None

                for [key, value] in lines:
                    if key == "nameserver":
                        dns_server = value
                    elif key == "searchdomain":
                        dns_search = value

                for [key, value] in lines:
                    if key.startswith("ipconfig"):
                        print(f"Considering {key} for VM {vm_id}", file=sys.stderr)
                        net_id = int(key[len("ipconfig") :])
                        net_conf = parse_kv(options[f"net{net_id}"])

                        if net_conf.get("firewall", "0") == "1":
                            interface = f"fwbr{vm_id}i{net_id}"
                            if_raw = interface
                            tag = 0
                        elif "tag" in net_conf:
                            tag = int(net_conf["tag"])
                            interface = f"{net_conf['bridge']}.{tag}"
                            if_raw = net_conf["bridge"]
                        else:
                            interface = net_conf["bridge"]
                            if_raw = interface
                            tag = 0

                        ip_config = parse_kv(value)

                        address = ipaddress.ip_interface(ip_config["ip"])
                        gateway = (
                            ipaddress.ip_address(ip_config["gw"])
                            if "gw" in ip_config
                            else None
                        )

                        results[interface] = results.get(
                            interface,
                            {
                                "bridge": interface,
                                "bridge_raw": if_raw,
                                "vlan": tag,
                                "subnet": address.network,
                                "gateway": gateway,
                                "reservations": []
                            },
                        )
                        if_opts = results[interface]

                        if not address.ip in if_opts["subnet"]:
                            errors.append(
                                f'VM ID {vm_id} network interface {net_id} has an IP assigned of {str(address.ip)}, which does not reside in {if_opts["subnet"]} previously defined as used for network {interface}'
                            )

                        if if_opts["gateway"] != gateway:
                            errors.append(
                                f'VM ID {vm_id} network interface {net_id} has an gateway with a value of {str(gateway)}, which does not match the gateway for {interface} of {if_opts["gateway"]}'
                            )

                        if_opts["reservations"].append(
                            {
                                "vmid": vm_id,
                                "interface": net_id,
                                "mac": next(
                                    mac
                                    for mac in net_conf.values()
                                    if re.match(r"([A-F0-9]{2}:){5}[A-F0-9]{2}", mac)
                                ),
                                "ip": address.ip,
                                "dns_search": dns_search,
                                "dns_server": dns_server,
                            }
                        )
        except Exception as e:
            print(f"Failed to check VM {vm_id} for info: ", e, file=sys.stderr)
            errors.append(f"Failed to check VM {vm_id} for info: : {e.message}")

    return results


def run_cmd(cmd, exit_on_failure=True):
    print("Running command: ", cmd, file=sys.stderr)
    if os.system(cmd) != 0 and exit_on_failure:
        raise Exception("Failed to run command specified! " + cmd)


def update_reservations():
    print("Checking to update reservations...", file=sys.stderr)

    try:
        raw_query.clear()
        new_reservations = query_reservations()
        raw_query.extend(new_reservations.values())

        for new_res in new_reservations.values():
            res_list = [Reservation(**r) for r in new_res["reservations"]]

            for interface in interfaces:
                if interface.interface == new_res["bridge"]:
                    old_config = interface.build_config()
                    new_config = interface.build_config(res_list)

                    if old_config != new_config:
                        interface.reservations = res_list
                        interface.rebuild = True
                        interface.status = "Pending rebuild"

                    break
            else:
                interface = InterfaceReservations()
                interface.interface = new_res["bridge"]
                interface.if_raw = new_res["bridge_raw"]
                interface.vlan = new_res["vlan"]
                interface.subnet = new_res["subnet"]
                interface.gateway = new_res["gateway"]
                interface.reservations = res_list
                interface.allocated_reservations = []
                interfaces.append(interface)

        for interface in interfaces:
            for new_res in new_reservations.values():
                if interface.interface == new_res["bridge"]:
                    break
            else:
                interface.stop()
                interface.status = "No longer needed"
                print(f"Stopped DHCP on {interface.interface}", file=sys.stderr)

        for i, interface in enumerate(interfaces):
            if not interface.rebuild:
                continue

            print(f"Rebuilding {interface.interface}...", file=sys.stderr)

            try:
                interface.stop()
                interfaces[i] = interface.rebuild_if()
                interface = interfaces[i]
                print(f"Stopped DHCP on {interface.interface}", file=sys.stderr)

                os.makedirs(f"/etc/pkci/{interface.interface}", exist_ok=True)

                run_cmd(f"ip netns add kea_{interface.interface}")
                run_cmd(f"ip link del kh_{interface.interface}", exit_on_failure=False)
                run_cmd(
                    f"ip link add kh_{interface.interface} type veth peer kn_{interface.interface}"
                )
                run_cmd(
                    f"ip link set kn_{interface.interface} netns kea_{interface.interface}"
                )
                run_cmd(f"ip -n kea_{interface.interface} link set lo up")
                run_cmd(
                    f"ip -n kea_{interface.interface} link set kn_{interface.interface} up"
                )
                run_cmd(
                    f"ip -n kea_{interface.interface} addr add {str(interface.subnet[-2])}/{interface.subnet.prefixlen} brd + dev kn_{interface.interface}"
                )
                if interface.vlan != 0:
                    run_cmd(
                        f"ip link set kh_{interface.interface} master {interface.if_raw}"
                    )
                else:
                    run_cmd(
                        f"ip link set kh_{interface.interface} master {interface.interface}"
                    )
                run_cmd(f"ip link set kh_{interface.interface} up")

                if interface.vlan != 0:
                    run_cmd(f"bridge vlan del vid 1 dev kh_{interface.interface}")
                    run_cmd(
                        f"bridge vlan add vid {interface.vlan} dev kh_{interface.interface} pvid untagged"
                    )

                interface.start()

                interface.rebuild = False
                interface.status = "Up and running"
            except Exception as e:
                print("Failed to create network: ", e, file=sys.stderr)
                errors.append(
                    f"Failed to rebuild interface {interface.interface}; {e.message}"
                )
                crash = e
                interface.status = "Failed to start"

    except Exception as e:
        print("Failed to check for new reservations: ", e, file=sys.stderr)
        errors.append(f"Failed to check for new reservations: {e.message}")
        crash = e

    # for res in new_reservations.values():
    #    print(f'Interface {res["bridge"]} ({res["subnet"]})')
    #    for r in res['reservations']:
    #        print(f'\t{r["vmid"]}: {r["ip"]} ({r["mac"]})')

    # print(json.dumps(errors, indent=4))


timer = RepeatTimer(int(os.environ.get("VM_CHECK_POLL", "30")), update_reservations)

if __name__ == "__main__":
    print("Running background thread to spawn DHCP servers")
    timer.start()

    print("Running server")
    server.run(host="0.0.0.0")
