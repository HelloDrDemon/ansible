# (c) 2016-2018, Matt Davis <mdavis@ansible.com>
# (c) 2018, Sam Doran <sdoran@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import random
import time

from datetime import datetime, timedelta

from ansible.errors import AnsibleError
from ansible.plugins.action import ActionBase
from ansible.module_utils._text import to_native, to_text


try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display
    display = Display()


class TimedOutException(Exception):
    pass


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    DEFAULT_REBOOT_TIMEOUT = 600
    DEFAULT_CONNECT_TIMEOUT = None
    DEFAULT_PRE_REBOOT_DELAY = 0
    DEFAULT_POST_REBOOT_DELAY = 0
    DEFAULT_TEST_COMMAND = 'whoami'
    DEFAULT_BOOT_TIME_COMMAND = 'who -b'
    DEFAULT_REBOOT_MESSAGE = 'Reboot initiated by Ansible'
    DEFAULT_SHUTDOWN_COMMAND = 'shutdown'
    DEFAULT_SUDOABLE = True

    DEPRECATED_ARGS = {}

    SHUTDOWN_COMMANDS = {
        'linux': DEFAULT_SHUTDOWN_COMMAND,
        'freebsd': DEFAULT_SHUTDOWN_COMMAND,
        'sunos': '/usr/sbin/shutdown',
        'darwin': '/sbin/shutdown',
    }

    SHUTDOWN_COMMAND_ARGS = {
        'linux': '-r {delay_min} "{message}"',
        'freebsd': '-r +{delay_sec}s "{message}"',
        'sunos': '-y -g {delay_sec} -r "{message}"',
        'darwin': '-r +{delay_min_macos} "{message}"'
    }

    def __init__(self, *args, **kwargs):
        super(ActionModule, self).__init__(*args, **kwargs)

        self._original_connection_timeout = None
        self._previous_boot_time = None

    def deprecated_args(self):
        for arg, version in self.DEPRECATED_ARGS.items():
            if self._task.args.get(arg) is not None:
                display.warning("Since Ansible %s, %s is no longer a valid option for %s" % (version, arg, self._task.action))

    def construct_command(self):
        # Determine the system distribution in order to use the correct shutdown command arguments
        uname_result = self._low_level_execute_command('uname')
        distribution = uname_result['stdout'].strip().lower()

        shutdown_command = self.SHUTDOWN_COMMANDS.get(distribution, self.SHUTDOWN_COMMAND_ARGS['linux'])
        shutdown_command_args = self.SHUTDOWN_COMMAND_ARGS.get(distribution, self.SHUTDOWN_COMMAND_ARGS['linux'])

        pre_reboot_delay = int(self._task.args.get('pre_reboot_delay', self.DEFAULT_PRE_REBOOT_DELAY))
        if pre_reboot_delay < 0:
            pre_reboot_delay = 0

        # Convert seconds to minutes for Linux. If less that 60, set it to 0 except for macOS which will
        # sever the connection too quickly if set to 0, so set that to 1.
        # We could simplify this by setting them both to 1, but I think of all the time that
        # people will lose waiting for that extra 1 minute delay and want to give them their
        # lives back.
        delay_min = pre_reboot_delay // 60
        delay_min_macos = delay_min | 1
        msg = self._task.args.get('msg', self.DEFAULT_REBOOT_MESSAGE)

        shutdown_command_args = shutdown_command_args.format(delay_sec=pre_reboot_delay, delay_min=delay_min, delay_min_macos=delay_min_macos, message=msg)

        reboot_command = '%s %s' % (shutdown_command, shutdown_command_args)
        return reboot_command

    def get_system_boot_time(self):
        command_result = self._low_level_execute_command(self.DEFAULT_BOOT_TIME_COMMAND, sudoable=self.DEFAULT_SUDOABLE)

        # For single board computers, e.g., Raspberry Pi, that lack a real time clock and are using fake-hwclock
        # launched by systemd, the update of utmp/wtmp is not done correctly.
        # Fall back to using uptime -s for those systems.
        # https://github.com/systemd/systemd/issues/6057
        if '1970-01-01 00:00' in command_result['stdout']:
            command_result = self._low_level_execute_command('uptime -s', sudoable=self.DEFAULT_SUDOABLE)

        if command_result['rc'] != 0:
            raise AnsibleError("%s: failed to get host boot time info, rc: %d, stdout: %s, stderr: %s"
                               % (self._task.action, command_result.rc, to_native(command_result['stdout']), to_native(command_result['stderr'])))

        return command_result['stdout'].strip()

    def check_boot_time(self):
        display.vvv("%s: attempting to get system boot time" % self._task.action)
        connect_timeout = self._task.args.get('connect_timeout', self._task.args.get('connect_timeout_sec', self.DEFAULT_CONNECT_TIMEOUT))

        # override connection timeout from defaults to custom value
        if connect_timeout:
            try:
                self._connection.set_option("connection_timeout", connect_timeout)
                self._connection.reset()
            except AttributeError:
                display.warning("Connection plugin does not allow the connection timeout to be overridden")

        # try and get boot time
        try:
            current_boot_time = self.get_system_boot_time()
        except Exception as e:
            raise e

        # FreeBSD returns an empty string immediately before reboot so adding a length
        # check to prevent prematurely assuming system has rebooted
        if len(current_boot_time) == 0 or current_boot_time == self._previous_boot_time:
            raise Exception("boot time has not changed")

    def run_test_command(self, **kwargs):
        test_command = self._task.args.get('test_command', self.DEFAULT_TEST_COMMAND)
        display.vvv("%s: attempting post-reboot test command '%s'" % (self._task.action, test_command))
        command_result = self._low_level_execute_command(test_command, sudoable=self.DEFAULT_SUDOABLE)

        result = {}
        if command_result['rc'] != 0:
            result['failed'] = True
            result['msg'] = 'test command failed: %s %s' % (to_native(command_result['stderr'], to_native(command_result['stdout'])))
        else:
            result['msg'] = to_native(command_result['stdout'])

        return result

    def do_until_success_or_timeout(self, action, reboot_timeout, action_desc):
        max_end_time = datetime.utcnow() + timedelta(seconds=reboot_timeout)

        fail_count = 0
        max_fail_sleep = 12

        while datetime.utcnow() < max_end_time:
            try:
                action()
                if action_desc:
                    display.debug('%s: %s success' % (self._task.action, action_desc))
                return
            except Exception as e:
                # Use exponential backoff with a max timout, plus a little bit of randomness
                random_int = random.randint(0, 1000) / 1000
                fail_sleep = 2 ** fail_count + random_int
                if fail_sleep > max_fail_sleep:

                    fail_sleep = max_fail_sleep + random_int
                if action_desc:
                    display.debug("{0}: {1} fail '{2}', retrying in {3:.4} seconds...".format(self._task.action, action_desc, to_text(e), fail_sleep))
                fail_count += 1
                time.sleep(fail_sleep)

        raise TimedOutException('Timed out waiting for %s' % (action_desc))

    def perform_reboot(self):
        display.debug("%s: rebooting server" % self._task.action)

        remote_command = self.construct_command()
        reboot_result = self._low_level_execute_command(remote_command, sudoable=self.DEFAULT_SUDOABLE)
        result = {}
        result['start'] = datetime.utcnow()

        if reboot_result['rc'] != 0:
            result['failed'] = True
            result['rebooted'] = False
            result['msg'] = "Shutdown command failed. Error was %s, %s" % (
                to_native(reboot_result['stdout'].strip()), to_native(reboot_result['stderr'].strip()))
            return result

        result['failed'] = False

        # attempt to store the original connection_timeout option var so it can be reset after
        self._original_connection_timeout = None
        try:
            self._original_connection_timeout = self._connection.get_option('connection_timeout')
        except AnsibleError:
            display.debug("%s: connect_timeout connection option has not been set" % self._task.action)

        post_reboot_delay = int(self._task.args.get('post_reboot_delay', self._task.args.get('post_reboot_delay_sec', self.DEFAULT_POST_REBOOT_DELAY)))
        if post_reboot_delay < 0:
            post_reboot_delay = 0

        if post_reboot_delay != 0:
            display.vvv("%s: waiting an additional %d seconds" % (self._task.action, post_reboot_delay))
            time.sleep(post_reboot_delay)

        return result

    def validate_reboot(self):
        display.debug('%s: Validating reboot' % self._task.action)
        result = {}

        try:
            # keep on checking system boot_time with short connection responses
            reboot_timeout = int(self._task.args.get('reboot_timeout', self._task.args.get('reboot_timeout_sec', self.DEFAULT_REBOOT_TIMEOUT)))
            connect_timeout = self._task.args.get('connect_timeout', self._task.args.get('connect_timeout_sec', self.DEFAULT_CONNECT_TIMEOUT))
            self.do_until_success_or_timeout(self.check_boot_time, reboot_timeout, action_desc="boot_time check")

            if connect_timeout:
                # reset the connection to clear the custom connection timeout
                try:
                    self._connection.set_option("connection_timeout", connect_timeout)
                    self._connection.reset()
                except (AnsibleError, AttributeError) as e:
                    display.debug("Failed to reset connection_timeout back to default: %s" % to_text(e))

            # finally run test command to ensure everything is working
            # FUTURE: add a stability check (system must remain up for N seconds) to deal with self-multi-reboot updates
            self.do_until_success_or_timeout(self.run_test_command, reboot_timeout, action_desc="post-reboot test command")

            result['rebooted'] = True
            result['changed'] = True

        except TimedOutException as toex:
            result['failed'] = True
            result['rebooted'] = True
            result['msg'] = to_text(toex)
            return result

        return result

    def run(self, tmp=None, task_vars=None):
        self._supports_check_mode = True
        self._supports_async = True

        # If running with local connection, fail so we don't reboot ourself
        if self._connection.transport == 'local':
            msg = 'Running {0} with local connection would reboot the control node.'.format(self._task.action)
            return dict(changed=False, elapsed=0, rebooted=False, failed=True, msg=msg)

        if self._play_context.check_mode:
            return dict(changed=True, elapsed=0, rebooted=True)

        if task_vars is None:
            task_vars = dict()

        self.deprecated_args()

        result = super(ActionModule, self).run(tmp, task_vars)

        if result.get('skipped', False) or result.get('failed', False):
            return result

        # Get current boot time
        try:
            self._previous_boot_time = self.get_system_boot_time()
        except Exception as e:
            result['failed'] = True
            result['reboot'] = False
            result['msg'] = to_text(e)
            return result

        # Initiate reboot
        reboot_result = self.perform_reboot()

        if reboot_result['failed']:
            result = reboot_result
            elapsed = datetime.utcnow() - reboot_result['start']
            result['elapsed'] = elapsed.seconds
            return result

        # Make sure reboot was successful
        result = self.validate_reboot()

        elapsed = datetime.utcnow() - reboot_result['start']
        result['elapsed'] = elapsed.seconds

        return result
