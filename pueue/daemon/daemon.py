import os
import sys
import stat
import socket
import pickle
import select
import configparser
from copy import deepcopy

from pueue.daemon.files import cleanup

from pueue.daemon.queue import Queue
from pueue.daemon.logger import Logger
from pueue.daemon.process_handler import ProcessHandler


class Daemon():
    """The pueue daemon class.

    This is the central piece of code, which contains all client<->daemon
    communication code. The daemon manages the processes and the queue
    with the help of two other classes `ProcessHandler` and `Queue`.
    """
    def __init__(self, root_dir=None):
        """Initializes the daemon.

        Creates all needed directories, reads previous pueue sessions
        and the configuration files.
        """
        self.initialize_directories(root_dir)
        # Initialize logger before you do anything else.
        # In case anything fails, we want to see something in our logs.
        self.read_config()
        self.logger = Logger(root_dir)

        try:
            # Get config and initialize Queue, Logger and ProcessHandler
            self.queue = Queue(self.config_dir)
            self.process_handler = ProcessHandler(self.queue, self.logger, self.config_dir)
            self.process_handler.set_max(int(self.config['default']['maxProcesses']))
        except:
            self.logger.exception()
            raise

        # Remove old log files
        self.logger.remove_old(self.config['log']['logTime'])

        try:
            # Create daemon socket, exit if this doesn't work
            self.create_socket()

            # Rotate logs and reset queue, if all items from the last session finished
            if self.queue.next() is None:
                self.logger.rotate(self.queue)
                self.queue.reset()
        except:
            self.logger.exception()
            raise

        # Flags for various behaviours
        self.paused = False
        self.running = True
        self.stopping = False
        self.reset = False
        self.remove_current = False

        # If there are still jobs in the queue the daemon might pause,
        # if this behavior is defined in the config file.
        # The old log file is being loaded as well.
        if len(self.queue) > 0 and not self.config['default']['resumeAfterStart']:
            self.paused = True

        # Variables for handling sockets and child process
        self.client_address = None
        self.client_socket = None
        self.process = None
        self.read_list = [self.socket]

    def create_socket(self):
        """Create a socket for the daemon, depending on the directory location.

        Args:
            config_dir (str): The absolute path to the config directory used by the daemon.

        Returns:
            socket.socket: The daemon socket. Clients connect to this socket.
        """

        socket_path = os.path.join(self.config_dir, 'pueue.sock')
        # Create Socket and exit with 1, if socket can't be created
        try:
            if os.path.exists(socket_path):
                os.remove(socket_path)
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(socket_path)
            self.socket.setblocking(0)
            self.socket.listen(0)
            # Set file permissions
            os.chmod(socket_path, stat.S_IRWXU)
        except:
            self.logger.error("Daemon couldn't socket. Aborting")
            self.logger.exception()
            sys.exit(1)

        return self.socket

    def initialize_directories(self, root_dir):
        """Create all directories needed for logs and configs."""
        if not root_dir:
            root_dir = os.path.expanduser('~')

        # Create config directory, if it doesn't exist
        self.config_dir = os.path.join(root_dir, '.config/pueue')
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir)

    def respond_client(self, answer):
        """Generic function to send an answer to the client."""
        response = pickle.dumps(answer, -1)
        self.client_socket.send(response)
        self.read_list.remove(self.client_socket)
        self.client_socket.close()

    def read_config(self):
        """Read a previous configuration file or create a new with default values."""
        config_file = os.path.join(self.config_dir, 'pueue.ini')
        self.config = configparser.ConfigParser()
        # Try to get configuration file and return it
        # If this doesn't work, a new default config file will be created
        if os.path.exists(config_file):
            try:
                self.config.read(config_file)
                return
            except:
                self.logger.error('Error while parsing config file. Deleting old config')
                self.logger.exception()

        self.config['default'] = {
            'stopAtError': True,
            'resumeAfterStart': False,
            'maxProcesses': 1,
        }
        self.config['log'] = {
            'logTime': 60*60*24*14,
        }
        self.write_config()

    def write_config(self):
        """Write the current configuration to the config file."""
        config_file = os.path.join(self.config_dir, 'pueue.ini')
        with open(config_file, 'w') as file_descriptor:
            self.config.write(file_descriptor)

    def main(self):
        """The main function containing the loop for communication and process management.

        This function is the heart of the daemon.
        It is responsible for:
        - Client communication
        - Calling the ProcessHandler API.
        - Logging
        - Cleanup on exit

        """
        try:
            while self.running:
                # Check for any finished processes
                if self.process_handler.check_finished():
                    self.logger.write(self.queue)

                if self.reset and self.process_handler.all_finished():
                    # Rotate log and reset queue
                    self.logger.rotate(self.queue)
                    self.queue.reset()
                    self.reset = False

                # Start next Process
                if not self.paused and not self.reset and self.running:
                    self.process_handler.check_for_new()

                # Create list for waitable objects
                readable, writable, failed = select.select(self.read_list, [], [], 1)
                for socket in readable:
                    if socket is self.socket:
                        # Listening for clients to connect.
                        # Client sockets are added to readlist to be processed.
                        try:
                            self.client_socket, self.client_address = self.socket.accept()
                            self.read_list.append(self.client_socket)
                        except:
                            self.logger.warning('Daemon rejected client')
                    else:
                        # Trying to receive instruction from client socket
                        try:
                            instruction = self.client_socket.recv(1048576)
                        except EOFError:
                            self.logger.warning('Client died while sending message, dropping received data.')
                            instruction = None

                        # Check for valid instruction
                        if instruction is not None:
                            # Check if received data can be unpickled.
                            try:
                                payload = pickle.loads(instruction)
                            except EOFError:
                                # Instruction is ignored if it can't be unpickled
                                self.logger.error('Received message is incomplete, dropping received data.')
                                self.read_list.remove(self.client_socket)
                                self.client_socket.close()
                                # Set invalid payload
                                payload = {'mode': ''}

                            functions = {
                                'add': self.add,
                                'remove': self.remove,
                                'switch': self.switch,
                                'send': self.pipe_to_process,
                                'status': self.send_status,
                                'start': self.start,
                                'pause': self.pause,
                                'stash': self.stash,
                                'enqueue': self.enqueue,
                                'restart': self.restart,
                                'stop': self.stop_process,
                                'kill': self.kill_process,
                                'reset': self.reset_everything,
                                'clear': self.clear,
                                'config': self.set_config,
                                'STOPDAEMON': self.stop_daemon,
                            }

                            if payload['mode'] in functions.keys():
                                self.logger.debug('Payload received:')
                                self.logger.debug(payload)
                                response = functions[payload['mode']](payload)

                                self.logger.debug('Sending payload:')
                                self.logger.debug(response)
                                self.respond_client(response)
                            else:
                                self.respond_client({'message': 'Unknown Command',
                                                    'status': 'error'})
        except:
            self.logger.exception()

        # Wait for killed or stopped processes to finish (cleanup)
        self.process_handler.wait_for_finish()
        # Close socket, clean everything up and exit
        self.socket.close()
        cleanup(self.config_dir)
        sys.exit(0)

    def stop_daemon(self, payload=None):
        """Kill current processes and initiate daemon shutdown.

        The daemon will shut down after a last check on all killed processes.
        """
        self.process_handler.kill_all()
        self.running = False

        return {'message': 'Pueue daemon shutting down',
                'status': 'success'}

    def set_config(self, payload):
        self.config['default'][payload['option']] = str(payload['value'])

        if payload['option'] == 'maxProcesses':
            self.process_handler.set_max(payload['value'])
        self.write_config()

        return {'message': 'Configuration successfully updated.',
                'status': 'success'}

    def pipe_to_process(self, payload):
        """Send something to stdin of a specific process."""
        message = payload['input']
        key = payload['key']
        self.process_handler.send_to_process(message, key)

    def send_status(self, payload):
        """Send the daemon status and the current queue for displaying."""
        answer = {}
        data = []
        # Get daemon status
        if self.paused:
            answer['status'] = 'paused'
        else:
            answer['status'] = 'running'

        # Add current queue or a message, that queue is empty
        if len(self.queue) > 0:
            data = deepcopy(self.queue.queue)
            # Remove stderr and stdout output for transfer
            # Some outputs are way to big for the socket buffer
            # and this is not needed by the client
            for key, item in data.items():
                if 'stderr' in item:
                    del item['stderr']
                if 'stdout' in item:
                    del item['stdout']
        else:
            data = 'Queue is empty'
        answer['data'] = data

        return answer

    def reset_everything(self, payload):
        """Kill all processes, delete the queue and clean everything up."""

        self.process_handler.kill_all()
        self.process_handler.wait_for_finish()
        self.reset = True

        answer = {'message': 'Resetting current queue', 'status': 'success'}
        return answer

    def clear(self, payload):
        """Clears queue from any `done` or `failed` entries.

        The log will be rotated once. Otherwise we would loose all logs from
        thoes finished processes.
        """

        self.logger.rotate(self.queue)
        self.queue.clear()
        self.logger.write(self.queue)

        answer = {'message': 'Finished entries have been removed.', 'status': 'success'}
        return answer

    def start(self, payload):
        """Start the daemon and all processes or only a specific process."""
        # Start a specific process, if we have a key in our payload
        if payload.get('key') is not None:
            success = self.process_handler.start_process(payload['key'])
            if success:
                answer = {'message': 'Process started.', 'status': 'success'}
            else:
                answer = {'message': 'No paused or queued process with this key.',
                          'status': 'error'}

        # Start a all processes and the daemon
        else:
            self.process_handler.start_all()
            if self.paused:
                self.paused = False
                answer = {'message': 'Daemon and all processes started.',
                          'status': 'success'}
            else:
                answer = {'message': 'Daemon already running, starting all processes.',
                          'status': 'success'}
        return answer

    def pause(self, payload):
        """Start the daemon and all processes or only a specific process."""
        # Pause a specific process, if we have a key in our payload
        if payload.get('key') is not None:
            success = self.process_handler.pause_process(payload['key'])
            if success:
                answer = {'message': 'Process paused.', 'status': 'success'}
            else:
                answer = {'message': 'No running process with this key.',
                          'status': 'error'}

        # Pause all processes and the daemon
        else:
            if payload.get('wait'):
                self.paused = True
                answer = {'message': 'Pausing daemon, but waiting for processes to finish.',
                          'status': 'success'}
            else:
                self.process_handler.pause_all()
                if not self.paused:
                    self.paused = True
                    answer = {'message': 'Daemon and all processes paused.',
                              'status': 'success'}
                else:
                    answer = {'message': 'Daemon already paused, pausing all processes anyway.',
                              'status': 'success'}

        return answer

    def stash(self, payload):
        """Stash the specified process."""
        # Pause a specific process, if we have a key in our payload
        key = payload['key']
        if self.queue.get(key) is not None:
            if self.queue[key]['status'] == 'queued':
                self.queue[key]['status'] = 'stashed'
                answer = {'message': 'Process stashed.', 'status': 'success'}
            else:
                answer = {'message': 'The specified entry is not queued.',
                          'status': 'error'}
        else:
            answer = {'message': 'No entry for this key.',
                      'status': 'error'}

        return answer

    def enqueue(self, payload):
        """Enqueue a stashed process."""
        # Pause a specific process, if we have a key in our payload
        key = payload['key']
        if self.queue.get(key) is not None:
            if self.queue[key]['status'] == 'stashed':
                self.queue[key]['status'] = 'queued'
                answer = {'message': 'Process enqueued.', 'status': 'success'}
            else:
                answer = {'message': 'The specified entry is not stashed.',
                          'status': 'error'}
        else:
            answer = {'message': 'No entry for this key.',
                      'status': 'error'}

        return answer

    def stop_process(self, payload):
        """Pause the daemon and stop all processes or stop a specific process."""
        # Stop a specific process, if we have a key in our payload
        if payload.get('key') is not None:
            # Mark the process as `to be removed` as soon as it terminates
            if payload.get('remove'):
                success = self.process_handler.stop_process(payload['key'], remove=True)
                success_message = 'Process will be stopped and removed.'
            else:
                success = self.process_handler.stop_process(payload['key'], stash=True)
                success_message = 'Process stopping.'

            if success:
                answer = {'message': success_message, 'status': 'success'}
            else:
                answer = {'message': 'No running process with this key.',
                          'status': 'error'}

        # Stop all processes and the daemon
        else:
            self.process_handler.stop_all()
            if not self.paused:
                self.paused = True
                answer = {'message': 'Daemon paused and all processes stopped.',
                          'status': 'success'}
            else:
                answer = {'message': 'Daemon already paused, stopping all processes.',
                          'status': 'success'}
        return answer

    def kill_process(self, payload):
        """Pause the daemon and kill all processes or kill a specific process."""
        # Kill a specific process, if we have a key in our payload
        if payload.get('key') is not None:
            # Mark the process as `to be removed` as soon as it terminates
            if payload.get('remove'):
                success = self.process_handler.kill_process(payload['key'], remove=True)
                success_message = 'Process will be killed and removed.'
            else:
                success = self.process_handler.kill_process(payload['key'], stash=True)
                success_message = 'Process killed.'

            if success:
                answer = {'message': success_message, 'status': 'success'}
            else:
                answer = {'message': 'No running process with this key.',
                          'status': 'error'}

        # Kill all processes and the daemon
        else:
            self.process_handler.kill_all()
            if not self.paused:
                self.paused = True
                answer = {'message': 'Daemon paused and all processes kill.',
                          'status': 'success'}
            else:

                answer = {'message': 'Daemon already paused, kill all processes.',
                          'status': 'success'}
        return answer

    def add(self, payload):
        """Add a entry to the queue."""
        self.queue.add_new(payload)
        return {'message': 'Command added', 'status': 'success'}

    def remove(self, payload):
        """Remove a single entry from the queue."""
        key = payload['key']
        running = self.process_handler.is_running(key)
        if running:
            answer = {
                'message': "Can't remove running process, "
                "please stop the process before removing it.",
                'status': 'error'
            }
        else:
            # Check if we can delete the command from the queue
            removed = self.queue.remove(key)
            if removed:
                answer = {'message': 'Command #{} removed'.format(key), 'status': 'success'}
            else:
                answer = {'message': 'No command with key #{}'.format(str(key)), 'status': 'error'}

        return answer

    def switch(self, payload):
        first = payload['first']
        second = payload['second']
        running = self.process_handler.is_running(first) or self.process_handler.is_running(second)
        if running:
            answer = {
                'message': "Can't switch running processes, "
                "please stop the processes before switching them.",
                'status': 'error'
            }

        else:
            switched = self.queue.switch(first, second)
            if switched:
                answer = {
                    'message': 'Command #{} and #{} switched'.format(first, second),
                    'status': 'success'
                }
            else:
                answer = {'message': "One of the specified keys doesn't exist in the queue.",
                          'status': 'error'}
        return answer

    def restart(self, payload):
        key = payload['key']
        restarted = self.queue.restart(key)
        if restarted:
            answer = {'message': 'Command #{} queued again'.format(key),
                      'status': 'success'}
        else:
            answer = {'message': 'No finished command for this key',
                      'status': 'error'}
        return answer
