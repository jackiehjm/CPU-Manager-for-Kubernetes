# Copyright (c) 2017 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from . import proc
from collections import OrderedDict
import json
import logging
import os
import subprocess

ENV_LSCPU_SYSFS = "CMK_DEV_LSCPU_SYSFS"


# Returns a dictionary of socket_id (int) to intel.topology.Socket.
def discover():
    isol = isolcpus()
    if isol:
        logging.info("Isolated logical cores: {}".format(
            ",".join([str(c) for c in isol])))
    return parse(lscpu(), isol)


class Platform:
    def __init__(self, sockets):
        self.sockets = sockets

    def has_isolated_cores(self):
        for socket in self.sockets.values():
            if socket.has_isolated_cores():
                return True
        return False

    def get_socket(self, id):
        if id not in self.sockets:
            return None
        return self.sockets[id]

    def get_cores(self, mode="packed"):
        return self.get_cores_general(mode, False)

    def get_isolated_cores(self, mode="packed"):
        return self.get_cores_general(mode, True)

    def get_cores_general(self, mode, isolated=False):
        if mode not in ["spread", "packed"]:
            logging.warning("Wrong mode has been selected."
                            "Fallback to vertical")
            mode = "packed"

        if mode == "packed":
            return self.allocate_packed(isolated)
        if mode == "spread":
            return self.allocate_spread(isolated)

    def allocate_packed(self, isolated_cores=False):
        cores = []
        for socket in self.sockets.values():
            if isolated_cores:
                cores += socket.get_isolated_cores()
            else:
                cores += socket.get_cores()
        return cores

    def allocate_spread(self, isolated_cores=False):
        output_cores = []
        socket_cores = {}

        for socket in self.sockets:
            if isolated_cores:
                socket_cores[socket] = self.sockets[socket]\
                    .get_isolated_cores()
            else:
                socket_cores[socket] = self.sockets[socket].get_cores()
        while len(socket_cores) > 0:
            sockets = [socket for socket in socket_cores]
            for socket in sockets:
                if len(socket_cores[socket]) == 0:
                    del(socket_cores[socket])
                    continue
                output_cores.append(socket_cores[socket][0])
                del(socket_cores[socket][0])

        return output_cores

    def get_shared_cores(self):
        cores = []
        for socket in self.sockets.values():
            cores += socket.get_shared_cores()
        return cores

    def get_cores_from_pool(self, pool):
        cores = []
        for socket in self.sockets.values():
            cores += socket.get_cores_from_pool(pool)
        return cores


class Socket:
    def __init__(self, socket_id, cores=None):
        if not cores:
            cores = {}
        self.socket_id = socket_id
        self.cores = OrderedDict(
            sorted(cores.items(), key=lambda pair: pair[1].core_id))

    def has_isolated_cores(self):
        for core in self.cores.values():
            if core.is_isolated():
                return True
        return False

    def get_cores(self):
        return [core for core in self.cores.values()]

    def get_isolated_cores(self):
        return [core for core in self.cores.values() if core.is_isolated()]

    def get_shared_cores(self):
        return [core for core in self.cores.values() if not core.is_isolated()]

    def get_cores_from_pool(self, pool):
        return [core for core in self.cores.values() if core.pool == pool]

    def as_dict(self, include_pool=True):
        return {
            "id": self.socket_id,
            "cores": [c.as_dict(include_pool) for c in self.cores.values()]
        }

    def json(self):
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


class Core:
    def __init__(self, core_id, cpus=None):
        if not cpus:
            cpus = {}
        self.core_id = core_id
        self.pool = None
        self.cpus = OrderedDict(
            sorted(cpus.items(), key=lambda pair: pair[1].cpu_id))

    def cpu_ids(self):
        return list(self.cpus.keys())

    def is_isolated(self):
        if len(self.cpus) == 0:
            return False

        for cpu_id in self.cpus:
            if not self.cpus[cpu_id].isolated:
                return False

        return True

    def as_dict(self, include_pool=True):
        result = {
            "id": self.core_id,
            "cpus": [c.as_dict() for c in self.cpus.values()]
        }

        if include_pool:
            result["pool"] = self.pool

        return result


class CPU:
    def __init__(self, cpu_id):
        self.cpu_id = cpu_id
        self.isolated = False

    def as_dict(self):
        return {
            "id": self.cpu_id,
            "isolated": self.isolated,
        }


# Returns of map of socket id (integer) to sockets (Socket type).
# lscpu has the following format:
# # The following is the parsable format, which can be fed to other
# # programs. Each different item in every column has an unique ID
# # starting from zero.
# # CPU,Core,Socket,Node,,L1d,L1i,L2,L3
# 0,0,0,0,,0,0,0,0
# 1,1,0,0,,1,1,1,0
def parse(lscpu_output, isolated_cpus=None):
    if not isolated_cpus:
        isolated_cpus = []

    sockets = {}

    for line in lscpu_output.split("\n"):
        if line and not line.startswith("#"):
            cpuinfo = line.split(",")

            socket_id = int(cpuinfo[2])
            core_id = int(cpuinfo[1])
            cpu_id = int(cpuinfo[0])

            if socket_id not in sockets:
                sockets[socket_id] = Socket(socket_id)

            socket = sockets[socket_id]

            if core_id not in socket.cores:
                socket.cores[core_id] = Core(core_id)
            core = socket.cores[core_id]

            cpu = CPU(cpu_id)
            if cpu.cpu_id in isolated_cpus:
                cpu.isolated = True
            core.cpus[cpu_id] = cpu

    return Platform(sockets)


def lscpu():
    sys_fs_path = os.getenv(ENV_LSCPU_SYSFS)
    if sys_fs_path is None:
        cmd_out = subprocess.check_output("lscpu -p", shell=True)
    else:
        cmd_out = subprocess.check_output(
            "lscpu -p -s %s" % sys_fs_path, shell=True)

    return cmd_out.decode("UTF-8")


def isolcpus():
    with open(os.path.join(proc.procfs(), "cmdline")) as f:
        return parse_isolcpus(f.read())


# Returns list of isolated cpu ids from /proc/cmdline content.
def parse_isolcpus(cmdline):
    cpus = []
    isol_cpus = []
    nocbs_cpus = []

    # Ensure that newlines are removed.
    cmdline_stripped = cmdline.rstrip()

    cmdline_fields = cmdline_stripped.split()

    isol_str = ""
    nocbs_str = ""
    for cmdline_field in cmdline_fields:
        pair = cmdline_field.split("=")
        if len(pair) != 2:
            continue

        key = pair[0]
        value = pair[1]

        if key == "isolcpus":
            isol_str = value.split(",")
            isol_cpus += parse_cpus_from_isolcpus(isol_str)
        if key == "rcu_nocbs":
            nocbs_str = value.split(",")
            nocbs_cpus += parse_cpus_from_isolcpus(nocbs_str)

    if isol_cpus and nocbs_cpus:
        cpus += [x for x in nocbs_cpus if x not in isol_cpus]

    # Get unique cpu_ids from list
    cpus = list(set(cpus))
    return cpus


def parse_cpus_from_isolcpus(cpus_str):
    cpus = []
    for cpu_id in cpus_str:
        if "-" not in cpu_id:
            cpus.append(int(cpu_id))
            continue
        cpu_range = cpu_id.split("-")
        if len(cpu_range) != 2:
            continue
        cpus += range(int(cpu_range[0]), int(cpu_range[1])+1)
    return cpus
