import logging
import logging.config
from typing import Type

from nornir.core.configuration import Config
from nornir.core.connections import ConnectionPlugin
from nornir.core.scheduler import TaskScheduler
from nornir.core.task import Task
from nornir.plugins import connections


class Data(object):
    """
    This class is just a placeholder to share data amongst different
    versions of Nornir after running ``filter`` multiple times.

    Attributes:
        failed_hosts (list): Hosts that have failed to run a task properly
        available_connections (dict): Dictionary holding available connection plugins
    """

    def __init__(self):
        self.failed_hosts = set()
        self.available_connections = connections.available_connections

    def recover_host(self, host):
        """Remove ``host`` from list of failed hosts."""
        self.failed_hosts.discard(host)

    def reset_failed_hosts(self):
        """Reset failed hosts and make all hosts available for future tasks."""
        self.failed_hosts = set()

    def to_dict(self):
        """ Return a dictionary representing the object. """
        return self.__dict__


class Nornir(object):
    """
    This is the main object to work with. It contains the inventory and it serves
    as task dispatcher.

    Arguments:
        inventory (:obj:`nornir.core.inventory.Inventory`): Inventory to work with
        data(:obj:`nornir.core.Data`): shared data amongst different iterations of nornir
        dry_run(``bool``): Whether if we are testing the changes or not
        config (:obj:`nornir.core.configuration.Config`): Configuration object
        config_file (``str``): Path to Yaml configuration file
        available_connections (``dict``): dict of connection types that will be made available.
            Defaults to :obj:`nornir.plugins.tasks.connections.available_connections`

    Attributes:
        inventory (:obj:`nornir.core.inventory.Inventory`): Inventory to work with
        data(:obj:`nornir.core.Data`): shared data amongst different iterations of nornir
        dry_run(``bool``): Whether if we are testing the changes or not
        config (:obj:`nornir.core.configuration.Config`): Configuration parameters
    """

    def __init__(
        self,
        inventory,
        dry_run,
        config=None,
        config_file=None,
        available_connections=None,
        logger=None,
        data=None,
    ):
        self.logger = logger or logging.getLogger("nornir")

        self.data = data or Data()
        self.inventory = inventory
        self.inventory.nornir = self
        self.data.dry_run = dry_run

        if config_file:
            self.config = Config(config_file=config_file)
        else:
            self.config = config or Config()

        self.configure_logging()

        if available_connections is not None:
            self.data.available_connections = available_connections

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close_connections(on_good=True, on_failed=True)

    @property
    def dry_run(self):
        return self.data.dry_run

    def configure_logging(self):
        dictConfig = self.config.logging_dictConfig or {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"simple": {"format": self.config.logging_format}},
            "handlers": {},
            "loggers": {},
            "root": {
                "level": "CRITICAL"
                if self.config.logging_loggers
                else self.config.logging_level.upper(),  # noqa
                "handlers": [],
                "formatter": "simple",
            },
        }
        handlers_list = []
        if self.config.logging_file:
            dictConfig["root"]["handlers"].append("info_file_handler")
            handlers_list.append("info_file_handler")
            dictConfig["handlers"]["info_file_handler"] = {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "NOTSET",
                "formatter": "simple",
                "filename": self.config.logging_file,
                "maxBytes": 10485760,
                "backupCount": 20,
                "encoding": "utf8",
            }
        if self.config.logging_to_console:
            dictConfig["root"]["handlers"].append("info_console")
            handlers_list.append("info_console")
            dictConfig["handlers"]["info_console"] = {
                "class": "logging.StreamHandler",
                "level": "NOTSET",
                "formatter": "simple",
                "stream": "ext://sys.stdout",
            }

        for logger in self.config.logging_loggers:
            dictConfig["loggers"][logger] = {
                "level": self.config.logging_level.upper(),
                "handlers": handlers_list,
            }

        if dictConfig["root"]["handlers"]:
            logging.config.dictConfig(dictConfig)

    def filter(self, *args, **kwargs):
        """
        See :py:meth:`nornir.core.inventory.Inventory.filter`

        Returns:
            :obj:`Nornir`: A new object with same configuration as ``self`` but filtered inventory.
        """
        b = Nornir(dry_run=self.dry_run, **self.__dict__)
        b.inventory = self.inventory.filter(*args, **kwargs)
        return b

    def run(
        self,
        task,
        num_workers=None,
        raise_on_error=None,
        on_good=True,
        on_failed=False,
        **kwargs
    ):
        """
        Run task over all the hosts in the inventory.

        Arguments:
            task (``callable``): function or callable that will be run against each device in
              the inventory
            num_workers(``int``): Override for how many hosts to run in paralell for this task
            raise_on_error (``bool``): Override raise_on_error behavior
            on_good(``bool``): Whether to run or not this task on hosts marked as good
            on_failed(``bool``): Whether to run or not this task on hosts marked as failed
            **kwargs: additional argument to pass to ``task`` when calling it

        Raises:
            :obj:`nornir.core.exceptions.NornirExecutionError`: if at least a task fails
              and self.config.raise_on_error is set to ``True``

        Returns:
            :obj:`nornir.core.task.AggregatedResult`: results of each execution
        """
        num_workers = num_workers or self.config.num_workers

        run_on = []
        if on_good:
            for name, host in self.inventory.hosts.items():
                if name not in self.data.failed_hosts:
                    run_on.append(host)
        if on_failed:
            for name, host in self.inventory.hosts.items():
                if name in self.data.failed_hosts:
                    run_on.append(host)

        self.logger.info(
            "Running task '{}' with num_workers: {}".format(
                kwargs.get("name") or task.__name__, num_workers
            )
        )
        self.logger.debug(kwargs)

        t = Task(task, nornir=self, **kwargs)
        scheduler = TaskScheduler(t, run_on)
        if num_workers == 1:
            result = scheduler.run_serial()
        else:
            result = scheduler.run_parallel(num_workers)

        raise_on_error = (
            raise_on_error if raise_on_error is not None else self.config.raise_on_error
        )
        if raise_on_error:
            result.raise_on_error()
        else:
            self.data.failed_hosts.update(result.failed_hosts.keys())
        return result

    def to_dict(self):
        """ Return a dictionary representing the object. """
        return {"data": self.data.to_dict(), "inventory": self.inventory.to_dict()}

    def get_connection_type(self, connection: str) -> Type[ConnectionPlugin]:
        """Returns the class for the given connection type."""
        return self.data.available_connections[connection]

    def close_connections(self, on_good=True, on_failed=False):
        def close_connections_task(task):
            task.host.close_connections()

        self.run(task=close_connections_task, on_good=on_good, on_failed=on_failed)


def InitNornir(config_file="", dry_run=False, **kwargs):
    """
    Arguments:
        config_file(str): Path to the configuration file (optional)
        dry_run(bool): Whether to simulate changes or not
        **kwargs: Extra information to pass to the
            :obj:`nornir.core.configuration.Config` object

    Returns:
        :obj:`nornir.core.Nornir`: fully instantiated and configured
    """
    conf = Config(config_file=config_file, **kwargs)

    inv_class = conf.inventory
    inv_args = getattr(conf, inv_class.__name__, {})
    transform_function = conf.transform_function
    inv = inv_class(transform_function=transform_function, **inv_args)

    return Nornir(inventory=inv, dry_run=dry_run, config=conf)
