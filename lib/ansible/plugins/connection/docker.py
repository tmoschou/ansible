# Based on the chroot connection plugin by Maykel Moya
#
# Connection plugin for configuring docker containers
# (c) 2014, Lorin Hochstein
# (c) 2015, Leendert Brouwer
#
# Maintainer: Leendert Brouwer (https://github.com/objectified)
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import os
import subprocess
import re

from distutils.version import LooseVersion

import ansible.constants as C

from ansible.errors import AnsibleError, AnsibleFileNotFound
from ansible.plugins.connection import ConnectionBase

BUFSIZE = 65536

class Connection(ConnectionBase):

    has_pipelining = True
    transport = 'docker'
    # su currently has an undiagnosed issue with calculating the file
    # checksums (so copy, for instance, doesn't work right)
    # Have to look into that before re-enabling this
    become_methods = frozenset(C.BECOME_METHODS).difference(('su',))

    def __init__(self, play_context, new_stdin, *args, **kwargs):
        super(Connection, self).__init__(play_context, new_stdin, *args, **kwargs)

        # Note: docker supports running as non-root in some configurations.
        # (For instance, setting the UNIX socket file to be readable and
        # writable by a specific UNIX group and then putting users into that
        # group).  But if the user is getting a permission denied error it
        # probably means that docker on their system is only configured to be
        # connected to by root and they are not running as root.
        if 'docker_command' in kwargs:
            self.docker_cmd = kwargs['docker_command']
        else:
            self.docker_cmd = 'docker'

        self.can_copy_bothways = False

        docker_version = self._get_docker_version()
        if LooseVersion(docker_version) < LooseVersion('1.3'):
            raise AnsibleError('docker connection type requires docker 1.3 or higher')
        if LooseVersion(docker_version) >= LooseVersion('1.8.0'):
            self.can_copy_bothways = True

    @staticmethod
    def _sanitize_version(version):
        return re.sub('[^0-9a-zA-Z\.]', '', version)

    def _get_docker_version(self):

        cmd = [self.docker_cmd, 'version']

        cmd_output = subprocess.check_output(cmd)

        for line in cmd_output.split('\n'):
            if line.startswith('Server version:'): # old docker versions
                return self._sanitize_version(line.split()[2])

        # no result yet, must be newer Docker version
        new_docker_cmd = [
            self.docker_cmd,
            'version', '--format', "'{{.Server.Version}}'"
        ]

        cmd_output = subprocess.check_output(new_docker_cmd)

        return self._sanitize_version(cmd_output)

    def _connect(self, port=None):
        """ Connect to the container. Nothing to do """
        super(Connection, self)._connect()
        if not self._connected:
            self._display.vvv("ESTABLISH LOCAL CONNECTION FOR USER: {0}".format(
                self._play_context.remote_user, host=self._play_context.remote_addr)
            )
            self._connected = True

    def exec_command(self, cmd, in_data=None, sudoable=False):
        """ Run a command on the local host """
        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)

        executable = C.DEFAULT_EXECUTABLE.split()[0] if C.DEFAULT_EXECUTABLE else '/bin/sh'
        # -i is needed to keep stdin open which allows pipelining to work
        local_cmd = [self.docker_cmd, "exec", '-i', self._play_context.remote_addr, executable, '-c', cmd]

        self._display.vvv("EXEC %s" % (local_cmd), host=self._play_context.remote_addr)
        p = subprocess.Popen(local_cmd,
                             shell=False,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        stdout, stderr = p.communicate(in_data)
        return (p.returncode, stdout, stderr)

    def put_file(self, in_path, out_path):
        """ Transfer a file from local to container """
        super(Connection, self).put_file(in_path, out_path)

        self._display.vvv("PUT %s TO %s" % (in_path, out_path), host=self._play_context.remote_addr)

        if not os.path.exists(in_path):
            raise AnsibleFileNotFound(
                "file or module does not exist: %s" % in_path)

        if self.can_copy_bothways:
            # only docker >= 1.8.1 can do this natively
            args = [
                self.docker_cmd,
                "cp",
                "%s" % in_path,
                "%s:%s" % (self._play_context.remote_addr, out_path)
            ]
            subprocess.check_call(args)
        else:
            # Older docker doesn't have native support for copying files into
            # running containers, so we use docker exec to implement this
            executable = C.DEFAULT_EXECUTABLE.split()[0] if C.DEFAULT_EXECUTABLE else '/bin/sh'
            args = [self.docker_cmd, "exec", "-i", self._play_context.remote_addr, executable, "-c",
                    "dd of={0} bs={1}".format(out_path, BUFSIZE)]
            with open(in_path, 'rb') as in_file:
                try:
                    p = subprocess.Popen(args, stdin=in_file,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except OSError:
                    raise AnsibleError("docker connection requires dd command in the chroot")
                stdout, stderr = p.communicate()

                if p.returncode != 0:
                    raise AnsibleError("failed to transfer file %s to %s:\n%s\n%s" % (in_path, out_path, stdout, stderr))

    def fetch_file(self, in_path, out_path):
        """ Fetch a file from container to local. """
        super(Connection, self).fetch_file(in_path, out_path)

        self._display.vvv("FETCH %s TO %s" % (in_path, out_path), host=self._play_context.remote_addr)

        # out_path is the final file path, but docker takes a directory, not a
        # file path
        out_dir = os.path.dirname(out_path)

        args = [self.docker_cmd, "cp", "%s:%s" % (self._play_context.remote_addr, in_path), out_dir]

        p = subprocess.Popen(args, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.communicate()

        # Rename if needed
        actual_out_path = os.path.join(out_dir, os.path.basename(in_path))
        if actual_out_path != out_path:
            os.rename(actual_out_path, out_path)

    def close(self):
        """ Terminate the connection. Nothing to do for Docker"""
        self._connected = False
