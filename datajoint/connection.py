"""
This module hosts the Connection class that manages the connection to the mysql database,
 and the `conn` function that provides access to a persistent connection in datajoint.
"""
import warnings
from contextlib import contextmanager
import pymysql as client
import logging

from . import config
from . import DataJointError
from .dependencies import Dependencies
from .jobs import JobManager
from pymysql import err

logger = logging.getLogger(__name__)


def conn(host=None, user=None, passwd=None, init_fun=None, reset=False):
    """
    Returns a persistent connection object to be shared by multiple modules.
    If the connection is not yet established or reset=True, a new connection is set up.
    If connection information is not provided, it is taken from config which takes the
    information from dj_local_conf.json. If the password is not specified in that file
    datajoint prompts for the password.

    :param host: hostname
    :param user: mysql user
    :param passwd: mysql password
    :param init_fun: initialization function
    :param reset: whether the connection should be reseted or not
    """
    if not hasattr(conn, 'connection') or reset:
        host = host if host is not None else config['database.host']
        user = user if user is not None else config['database.user']
        passwd = passwd if passwd is not None else config['database.password']
        if passwd is None:
            passwd = input("Please enter database password: ")
        init_fun = init_fun if init_fun is not None else config['connection.init_function']
        conn.connection = Connection(host, user, passwd, init_fun)
    return conn.connection


class Connection:
    """
    A dj.Connection object manages a connection to a database server.
    It also catalogues modules, schemas, tables, and their dependencies (foreign keys).

    Most of the parameters below should be set in the local configuration file.

    :param host: host name
    :param user: user name
    :param passwd: password
    :param init_fun: initialization function
    """

    def __init__(self, host, user, passwd, init_fun=None):
        if ':' in host:
            host, port = host.split(':')
            port = int(port)
        else:
            port = config['database.port']
        self.conn_info = dict(host=host, port=port, user=user, passwd=passwd)
        self.init_fun = init_fun
        self.connect()
        if self.is_connected:
            logger.info("Connected {user}@{host}:{port}".format(**self.conn_info))
        else:
            raise DataJointError('Connection failed.')
        self._conn.autocommit(True)
        self._in_transaction = False
        self.jobs = JobManager(self)
        self.schemas = dict()
        self.dependencies = Dependencies(self)

    def __eq__(self, other):
        return self.conn_info == other.conn_info

    def __repr__(self):
        connected = "connected" if self.is_connected else "disconnected"
        return "DataJoint connection ({connected}) {user}@{host}:{port}".format(
            connected=connected, **self.conn_info)

    def connect(self, init_fun=None):
        """
        Connects to the database server.

        :param init_fun: initialization function passed to pymysql
        """
        self._conn = client.connect(init_command=self.init_fun, **self.conn_info)

    def register(self, schema):
        self.schemas[schema.database] = schema

    @property
    def is_connected(self):
        """
        Returns true if the object is connected to the database server.
        """
        return self._conn.ping()

    def ping(self):
        """
        Keep the sql server connected
        """
        return self._conn.ping()

    def query(self, query, args=(), as_dict=False):
        """
        Execute the specified query and return the tuple generator (cursor).

        :param query: mysql query
        :param args: additional arguments for the client.cursor
        :param as_dict: If as_dict is set to True, the returned cursor objects returns
                        query results as dictionary.
        """
        cursor = client.cursors.DictCursor if as_dict else client.cursors.Cursor
        cur = self._conn.cursor(cursor=cursor)

        # Log the query
        try:
            logger.debug("Executing SQL:" + query[0:300])
            cur.execute(query, args)
        except err.OperationalError as e:
            if 'MySQL server has gone away' in str(e) and config['database.reconnect']:
                warnings.warn('''Mysql server has gone away.
                    Reconnected to the server. Data from transactions might be lost and referential constraints may
                    be violated. You can switch off this behavior by setting the 'database.reconnect' to False.
                    ''')
                self.connect()
            else:
                raise
        return cur

    # ---------- transaction processing
    @property
    def in_transaction(self):
        """
        :return: True if there is an open transaction.
        """
        self._in_transaction = self._in_transaction and self.is_connected
        return self._in_transaction

    def start_transaction(self):
        """
        Starts a transaction error.

        :raise DataJointError: if there is an ongoing transaction.
        """
        if self.in_transaction:
            raise DataJointError("Nested connections are not supported.")
        self.query('START TRANSACTION WITH CONSISTENT SNAPSHOT')
        self._in_transaction = True
        logger.info("Transaction started")

    def cancel_transaction(self):
        """
        Cancels the current transaction and rolls back all changes made during the transaction.

        """
        self.query('ROLLBACK')
        self._in_transaction = False
        logger.info("Transaction cancelled. Rolling back ...")

    def commit_transaction(self):
        """
        Commit all changes made during the transaction and close it.

        """
        self.query('COMMIT')
        self._in_transaction = False
        logger.info("Transaction committed and closed.")

    # -------- context manager for transactions
    @property
    @contextmanager
    def transaction(self):
        """
        Context manager for transactions. Opens an transaction and closes it after the with statement.
        If an error is caught during the transaction, the commits are automatically rolled back.
        All errors are raised again.

        Example:
        >>> import datajoint as dj
        >>> with dj.conn().transaction as conn:
        >>>     # transaction is open here
        """
        try:
            self.start_transaction()
            yield self
        except:
            self.cancel_transaction()
            raise
        else:
            self.commit_transaction()
