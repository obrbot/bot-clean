import asyncio
import time
import logging
import re
import os
import gc

import redis

from obrbot.connection import Connection
from obrbot.config import Config
from obrbot.plugin import PluginManager
from obrbot.event import Event, CommandEvent, RegexEvent, EventType
from obrbot.clients.irc import IrcConnection

logger = logging.getLogger("bot")


def clean_name(n):
    """strip all spaces and capitalization
    :type n: str
    :rtype: str
    """
    return re.sub('\s+', '', n.lower())


class ObrBot:
    """
    :type start_time: float
    :type running: bool
    :type connections: list[Connection | IrcConnection]
    :type config: core.config.Config
    :type plugin_manager: PluginManager
    :type db: redis.StrictRedis
    :type loop: asyncio.events.AbstractEventLoop
    :type stopped_future: asyncio.Future
    :param: stopped_future: Future that will be given a result when the bot has stopped.
    """

    def __init__(self, loop=asyncio.get_event_loop()):
        # basic variables
        self.loop = loop
        self.start_time = time.time()
        self.running = True
        # future which will be called when the bot stops
        self.stopped_future = asyncio.Future(loop=self.loop)

        # stores each bot server connection
        self.connections = []

        # set up config
        self.config = Config(self)
        logger.debug("Config system initialised.")

        # setup db
        db_config = self.config.get('database')
        db_host = db_config.get('host', 'localhost')
        db_port = db_config.get('port', 6379)
        db_database = db_config.get('database', 0)
        logger.info("Connecting to redis at {}:{}/{}".format(db_host, db_port, db_database))
        self.db = redis.StrictRedis(host=db_host, port=db_port, db=db_database)
        logger.debug("Database system initialised.")

        # Bot initialisation complete
        logger.debug("Bot setup completed.")

        # create bot connections
        self.create_connections()

        self.plugin_manager = PluginManager(self)

    def run(self):
        """
        Starts ObrBot.
        This will load plugins, connect to IRC, and process input.
        :return: True if ObrBot should be restarted, False otherwise
        :rtype: bool
        """
        # Initializes the bot, plugins and connections
        self.loop.run_until_complete(self._init_routine())
        # Wait till the bot stops. The stopped_future will be set to True to restart, False otherwise
        restart = self.loop.run_until_complete(self.stopped_future)
        self.loop.close()
        return restart

    def create_connections(self):
        """ Create a BotConnection for all the networks defined in the config """
        for config in self.config['connections']:
            # strip all spaces and capitalization from the connection name
            name = clean_name(config['name'])
            nick = config['nick']
            server = config['connection']['server']
            port = config['connection'].get('port', 6667)

            self.connections.append(IrcConnection(self, name, nick, config=config,
                                                  server=server, port=port,
                                                  use_ssl=config['connection'].get('ssl', False)))
            logger.debug("[{}] Created connection.".format(name))

    @asyncio.coroutine
    def stop(self, reason=None, *, restart=False):
        """quits all networks and shuts the bot down"""
        logger.info("Stopping.")

        for connection in self.connections:
            if not connection.connected:
                # Don't quit a connection that hasn't connected
                continue
            logger.debug("[{}] Closing connection.".format(connection.name))

            connection.quit(reason)

        yield from asyncio.sleep(0.5)  # wait for 'QUIT' calls to take affect

        for connection in self.connections:
            if not connection.connected:
                # Don't close a connection that hasn't connected
                continue
            connection.close()

        yield from self.plugin_manager.run_shutdown_hooks()

        self.running = False
        # Give the stopped_future a result, so that run() will exit
        self.stopped_future.set_result(restart)

    @asyncio.coroutine
    def restart(self, reason=None):
        """shuts the bot down and restarts it"""
        yield from self.stop(reason=reason, restart=True)

    @asyncio.coroutine
    def _init_routine(self):
        # Load plugins
        yield from self.plugin_manager.load_all(os.path.abspath("plugins"))

        # If we we're stopped while loading plugins, cancel that and just stop
        if not self.running:
            logger.info("Killed while loading, exiting")
            return

        # Connect to servers
        yield from asyncio.gather(*[conn.connect() for conn in self.connections], loop=self.loop)

        # Run a manual garbage collection cycle, to clean up any unused objects created during initialization
        gc.collect()

    @asyncio.coroutine
    def process(self, event):
        """
        :type event: Event
        """
        first = []
        tasks = []
        command_prefix = event.conn.config.get('command_prefix', '.')

        # Raw IRC hook
        for raw_hook in self.plugin_manager.catch_all_triggers:
            # run catch-all coroutine hooks before all others - TODO: Make this a plugin argument
            if raw_hook.run_first:
                first.append(self.plugin_manager.launch(raw_hook, Event(hook=raw_hook, base_event=event)))
            else:
                tasks.append(self.plugin_manager.launch(raw_hook, Event(hook=raw_hook, base_event=event)))
        if event.irc_command in self.plugin_manager.raw_triggers:
            for raw_hook in self.plugin_manager.raw_triggers[event.irc_command]:
                if raw_hook.run_first:
                    first.append(self.plugin_manager.launch(raw_hook, Event(hook=raw_hook, base_event=event)))
                else:
                    tasks.append(self.plugin_manager.launch(raw_hook, Event(hook=raw_hook, base_event=event)))

        # Event hooks
        if event.type in self.plugin_manager.event_type_hooks:
            for event_hook in self.plugin_manager.event_type_hooks[event.type]:
                if event_hook.run_first:
                    first.append(self.plugin_manager.launch(event_hook, Event(hook=event_hook, base_event=event)))
                else:
                    tasks.append(self.plugin_manager.launch(event_hook, Event(hook=event_hook, base_event=event)))

        if event.type is EventType.message:
            # Commands
            if event.chan_name.lower() == event.nick.lower():  # private message, no command prefix
                command_re = r'(?i)^(?:[{}]?|{}[,;:]+\s+)([\w-]+)(?:$|\s+)(.*)'.format(command_prefix,
                                                                                       event.conn.bot_nick)
            else:
                command_re = r'(?i)^(?:[{}]|{}[,;:]+\s+)([\w-]+)(?:$|\s+)(.*)'.format(command_prefix,
                                                                                      event.conn.bot_nick)

            match = re.match(command_re, event.content)

            if match:
                command = match.group(1).lower()
                if command in self.plugin_manager.commands:
                    command_hook = self.plugin_manager.commands[command]
                    command_event = CommandEvent(hook=command_hook, text=match.group(2).strip(),
                                                 triggered_command=command, base_event=event)
                    if command_hook.run_first:
                        first.append(self.plugin_manager.launch(command_hook, command_event))
                    else:
                        tasks.append(self.plugin_manager.launch(command_hook, command_event))

            # Regex hooks
            for regex, regex_hook in self.plugin_manager.regex_hooks:
                match = regex.search(event.content)
                if match:
                    regex_event = RegexEvent(hook=regex_hook, match=match, base_event=event)
                    if regex_hook.run_first:
                        first.append(self.plugin_manager.launch(regex_hook, regex_event))
                    else:
                        tasks.append(self.plugin_manager.launch(regex_hook, regex_event))

        # Run the tasks
        yield from asyncio.gather(*first, loop=self.loop)
        yield from asyncio.gather(*tasks, loop=self.loop)
