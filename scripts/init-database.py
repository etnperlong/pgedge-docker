from dataclasses import dataclass
from enum import Enum
import json
import os
import sys
import time
from typing import Any, Optional, Tuple, TypedDict, List, Literal
import psycopg
from psycopg import Cursor

CLUSTER_CONF_FILE = "/home/pgedge/cluster.json"
INIT_STATUS_FILE = "/data/init-status.json"

SUPERUSER_PARAMETERS = ", ".join(
    [
        "commit_delay",
        "deadlock_timeout",
        "lc_messages",
        "log_duration",
        "log_error_verbosity",
        "log_executor_stats",
        "log_lock_waits",
        "log_min_duration_sample",
        "log_min_duration_statement",
        "log_min_error_statement",
        "log_min_messages",
        "log_parser_stats",
        "log_planner_stats",
        "log_replication_commands",
        "log_statement",
        "log_statement_sample_rate",
        "log_statement_stats",
        "log_temp_files",
        "log_transaction_sample_rate",
        "pg_stat_statements.track",
        "pg_stat_statements.track_planning",
        "pg_stat_statements.track_utility",
        "session_replication_role",
        "temp_file_limit",
        "track_activities",
        "track_counts",
        "track_functions",
        "track_io_timing",
    ]
)


class NodeSpec(TypedDict):
    id: str
    name: str
    region: str
    hostname: Optional[str]
    internal_hostname: Optional[str]  # For backwards compatibility.


class UserSpec(TypedDict):
    username: str
    password: str
    superuser: Optional[bool]
    service: Literal["postgres", "pgcat"]
    type: Literal["application", "admin", "internal_admin", "pooler_auth", "other"]

class DatabaseSpec(TypedDict):
    name: str
    owner: Optional[str]
class ClusterSpec(TypedDict):
    name: str
    id: Optional[str]
    port: Optional[int]
    options: Optional[List[str]]
    nodes: List[NodeSpec]
    users: List[UserSpec]
    mode: Optional[str]
    self: Optional[NodeSpec]  # optional self reference
    databases: List[DatabaseSpec] # Multiple databases supported


def read_config() -> ClusterSpec:
    if not os.path.exists(CLUSTER_CONF_FILE):
        raise FileNotFoundError("spec not found")
    with open(CLUSTER_CONF_FILE) as f:
        return json.load(f)

class DatabaseStatus(str, Enum):
    CREATED = "created"      # Database is created but not initialized
    INITED = "inited"       # Database is initialized with extensions and basic setup
    SUBSCRIBED = "subscribed"  # Database has all peer subscriptions set up

class InitStatus(TypedDict):
    default_db_initialized: bool
    dbs_initialized: dict[str, DatabaseStatus]

def read_init_status():
    # Force update from enviroment
    force_update = os.getenv("FORCE_INIT", "false").lower() == "true"
    if force_update:
        info("WARNING: FORCE_INIT is set, forcing init status update")
        return {"default_db_initialized": False, "dbs_initialized": {}}
    if not os.path.exists(INIT_STATUS_FILE):
        return {"default_db_initialized": False, "dbs_initialized": {}}
    with open(INIT_STATUS_FILE) as f:
        # If not a valid json, return empty
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"default_db_initialized": False, "dbs_initialized": {}}
    
def update_database_init_status(database_name: str, status: DatabaseStatus) -> None:
    init_status = read_init_status()
    init_status["dbs_initialized"][database_name] = status
    with open(INIT_STATUS_FILE, "w") as f:
        json.dump(init_status, f)

def update_default_db_init_status(initialized: bool) -> None:
    init_status = read_init_status()
    init_status["default_db_initialized"] = initialized
    with open(INIT_STATUS_FILE, "w") as f:
        json.dump(init_status, f)

def info(*args) -> None:
    print("**** pgEdge:", *args, "****")
    sys.stdout.flush()


def connect(dsn: str, autocommit: bool = True):
    while True:
        try:
            return psycopg.connect(dsn, autocommit=autocommit)
        except psycopg.OperationalError as exc:
            info("unable to connect to database, retrying in 2 sec...", exc)
            time.sleep(2)


def can_connect(dsn: str) -> bool:
    try:
        psycopg.connect(dsn, connect_timeout=5)
        return True
    except psycopg.OperationalError:
        return False


def dsn(
    dbname: str,
    user: str,
    pw: Optional[str] = None,
    host: str = "localhost",
    port: int = 5432,
) -> str:
    fields = [
        f"host={host}",
        f"dbname={dbname}",
        f"user={user}",
        f"port={port}",
    ]
    if pw:
        fields.append(f"password={pw}")

    return " ".join(fields)


def wait_for_spock_node(dsn: str):
    with connect(dsn) as conn:
        with conn.cursor() as cursor:
            while True:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM spock.node;")
                    row = cursor.fetchone()
                    if row[0] > 0:
                        return
                except Exception as exc:
                    info("peer spock.node not configured, retrying in 2 sec...", exc)
                    time.sleep(2)


def spock_sub_create(cursor: Cursor, sub_name: str, other_dsn: str):
    forward_origins = "{}"
    replication_sets = "{default, default_insert_only, ddl_sql}"
    sub_create = f"""
    SELECT spock.sub_create(
        subscription_name := '{sub_name}',
        provider_dsn := '{other_dsn}',
        replication_sets := '{replication_sets}',
        forward_origins := '{forward_origins}',
        synchronize_structure := 'true',
        synchronize_data := 'true',
        apply_delay := '0'
    );"""
    # Retry until it works
    while True:
        try:
            cursor.execute(sub_create)
            return
        except Exception as exc:
            info("waiting for subscription to work...", exc)
            time.sleep(2)

def spock_sub_drop(cursor: Cursor, sub_name: str):
    sub_drop_if_exists = f"""
    SELECT spock.sub_drop(
        subscription_name := '{sub_name}',
        ifexists := 'true'
    );"""

    # Retry until it works
    while True:
        try:
            cursor.execute(sub_drop_if_exists)
            return
        except Exception as exc:
            info("waiting for subscription to drop...", exc)
            time.sleep(2)


def get_admin_creds(postgres_users: dict[str, Any]) -> Tuple[str, str]:
    for _, user in postgres_users.items():
        if user.get("service") == "postgres" and user["type"] == "admin":
            return user["username"], user["password"]
    return "", ""

def get_superuser_roles() -> str:
    pg_version = os.getenv("PGV")
    if pg_version == "15":
        return ", ".join(
            [
                "pg_read_all_data",
                "pg_write_all_data",
                "pg_read_all_settings",
                "pg_read_all_stats",
                "pg_stat_scan_tables",
                "pg_monitor",
                "pg_signal_backend",
                "pg_checkpoint",
            ]
        )
    elif pg_version in ["16", "17"]:
        return ", ".join(
            [
                "pg_read_all_data",
                "pg_write_all_data",
                "pg_read_all_settings",
                "pg_read_all_stats",
                "pg_stat_scan_tables",
                "pg_monitor",
                "pg_signal_backend",
                "pg_checkpoint",
                "pg_use_reserved_connections",
                "pg_create_subscription",
            ]
        )
    else:
        raise ValueError(f"unrecognized postgres version: '{pg_version}'")


def create_user_statement(user: UserSpec) -> list[str]:
    username = user["username"]
    password = user["password"]
    superuser = user.get("superuser")
    user_type = user.get("type")

    if superuser:
        return [f"CREATE USER {username} WITH LOGIN SUPERUSER PASSWORD '{password}';"]
    elif user_type in ["admin", "internal_admin"]:
        return [
            f"CREATE USER {username} WITH LOGIN CREATEROLE CREATEDB PASSWORD '{password}';",
            f"GRANT pgedge_superuser to {username} WITH ADMIN TRUE;",
        ]
    else:
        return [f"CREATE USER {username} WITH LOGIN PASSWORD '{password}';"]


def alter_user_statements(user: UserSpec, dbname: str, schemas: list[str]) -> list[str]:
    name = user["username"]
    stmts = [f"GRANT CONNECT ON DATABASE {dbname} TO {name};"]
    if user["type"] in ["application_read_only", "internal_read_only", "pooler_auth"]:
        for schema in schemas:
            stmts += [
                f"GRANT USAGE ON SCHEMA {schema} TO {name};",
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {name};",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {name};",
            ]
        if user["type"] == "internal_read_only":
            stmts.append(f"GRANT EXECUTE ON FUNCTION pg_ls_waldir TO {name};")
            stmts.append(f"GRANT pg_read_all_stats TO {name};")
        return stmts
    else:
        for schema in schemas:
            stmts += [
                f"GRANT USAGE, CREATE ON SCHEMA {schema} TO {name};",
                f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA {schema} TO {name};",
                f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {schema} TO {name};",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT ALL PRIVILEGES ON TABLES TO {name};",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT ALL PRIVILEGES ON SEQUENCES TO {name};",
            ]
        return stmts


def get_self_node(spec: ClusterSpec) -> NodeSpec:
    nodes = spec["nodes"]
    # Use the self entry from the spec if there is one
    if "self" in spec:
        return spec["self"]
    node_name = os.getenv("NODE_NAME", "n1")
    node_id = os.getenv("NODE_ID", "-1")
    # Find the node with that name
    self_node = next((node for node in nodes if node["id"] == node_id), None)
    if not self_node:
        info(f"ERROR: node {node_id} (name: {node_name}) not found in spec")
        sys.exit(1)
    return self_node


def get_hostname(node: NodeSpec) -> str:
    if "hostname" in node:
        return node["hostname"]
    # For backwards compatibility.
    return node["internal_hostname"]


@dataclass
class DatabaseInfo:
    database_name: str
    owner: str
    hostname: str
    mode: Optional[str]
    nodes: list[NodeSpec]
    node_id: str
    node_name: str
    postgres_users: dict[str, Any]
    spock_dsn: str
    local_dsn: str
    internal_dsn: str
    init_dsn: Optional[str]
    init_username: Optional[str]
    init_dbname: Optional[str]
    pgedge_pw: str


def get_default_db_info(spec: ClusterSpec) -> DatabaseInfo:
    default_db_name = spec.get("name")
    if not default_db_name:
        info("ERROR: default database name not found in spec")
        sys.exit(1)

    nodes = spec.get("nodes")
    if not nodes:
        info("ERROR: nodes not found in spec")
        sys.exit(1)

    users = spec.get("users")
    if not users:
        info("ERROR: users not found in spec")
        sys.exit(1)

    # Extract details of this node from the spec
    self_node = get_self_node(spec)
    hostname = get_hostname(self_node)
    node_name = self_node["name"]
    node_id = self_node["id"]
    postgres_users = dict(
        (user["username"], user) for user in users if user["service"] == "postgres"
    )

    # Get the pgedge password and remove the user from the dict.
    # This user already exists so we don't need to create it later.
    pgedge_pw = postgres_users.pop("pgedge", {}).get(
        "password", os.getenv("INIT_PASSWORD")
    )
    if not pgedge_pw:
        info("ERROR: pgedge user configuration not found in spec")
        sys.exit(1)
    admin_username, admin_password = get_admin_creds(postgres_users)
    if not admin_username or not admin_password:
        info("ERROR: admin user configuration not found in spec")
        sys.exit(1)

    # This DSN will be used for Spock subscriptions
    spock_dsn = dsn(dbname=default_db_name, user="pgedge", host=hostname, pw=pgedge_pw)

    # This DSN will be used for the admin connection
    local_dsn = dsn(dbname=default_db_name, user=admin_username, pw=admin_password)

    # This DSN will be used to the internal admin connection
    internal_dsn = dsn(dbname=default_db_name, user="pgedge", pw=pgedge_pw)

    # This DSN will be used to the internal admin connection
    init_dbname = os.getenv("INIT_DATABASE")
    init_username = os.getenv("INIT_USERNAME")
    init_password = os.getenv("INIT_PASSWORD")
    init_dsn = dsn(dbname=init_dbname, user=init_username, pw=init_password)

    # Deployment mode
    mode = spec.get("mode", "online")

    return DatabaseInfo(
        database_name=default_db_name,
        owner=admin_username,
        nodes=nodes,
        hostname=hostname,
        node_name=node_name,
        node_id=node_id,
        postgres_users=postgres_users,
        spock_dsn=spock_dsn,
        local_dsn=local_dsn,
        internal_dsn=internal_dsn,
        init_dsn=init_dsn,
        init_dbname=init_dbname,
        init_username=init_username,
        pgedge_pw=pgedge_pw,
        mode=mode,
    )

def get_dbs_info(spec: ClusterSpec) -> list[DatabaseInfo]:

    default_db_name = spec.get("name")
    if not default_db_name:
        info("ERROR: default database name not found in spec")
        sys.exit(1)

    databases = spec.get("databases")
    if not databases:
        info("WARNING: databases not found in spec, skipping other database initialization")
        return []
    
    # Get shared info
    nodes = spec.get("nodes")
    if not nodes:
        info("ERROR: nodes not found in spec")
        sys.exit(1)

    users = spec.get("users")
    if not users:
        info("ERROR: users not found in spec")
        sys.exit(1)

    # Extract details of this node from the spec
    self_node = get_self_node(spec)
    hostname = get_hostname(self_node)
    node_name = self_node["name"]
    node_id = self_node["id"]
    postgres_users = dict(
        (user["username"], user) for user in users if user["service"] == "postgres"
    )

    pgedge_pw = postgres_users.pop("pgedge", {}).get(
        "password", os.getenv("INIT_PASSWORD")
    )
    if not pgedge_pw:
        info("ERROR: pgedge user configuration not found in spec")
        sys.exit(1)
    admin_username, admin_password = get_admin_creds(postgres_users)
    if not admin_username or not admin_password:
        info("ERROR: admin user configuration not found in spec")
        sys.exit(1)

    # This DSN will be used to the internal admin connection
    init_dbname = os.getenv("INIT_DATABASE")
    init_username = os.getenv("INIT_USERNAME")
    init_password = os.getenv("INIT_PASSWORD")
    init_dsn = dsn(dbname=init_dbname, user=init_username, pw=init_password)
    
    # Deployment mode
    mode = spec.get("mode", "online")

    
    dbs_info = []
    for db in databases:
        db_name = db.get("name")
        db_owner = db.get("owner")
        if not db_owner:
            db_owner = admin_username
        
        # This DSN will be used for Spock subscriptions
        spock_dsn = dsn(dbname=db_name, user="pgedge", host=hostname, pw=pgedge_pw)

        # This DSN will be used for the admin connection
        local_dsn = dsn(dbname=db_name, user=admin_username, pw=admin_password)

        # This DSN will be used to the internal admin connection
        internal_dsn = dsn(dbname=default_db_name, user="pgedge", pw=pgedge_pw)

        db_info = DatabaseInfo(
            database_name=db_name,
            owner=db_owner,
            nodes=nodes,
            hostname=hostname,
            node_name=node_name,
            node_id=node_id,
            postgres_users=postgres_users,
            spock_dsn=spock_dsn,
            local_dsn=local_dsn,
            internal_dsn=internal_dsn,
            pgedge_pw=pgedge_pw,
            mode=mode,
            # Dummy init_dsn
            init_dsn=init_dsn,
            init_dbname=init_dbname,
            init_username=init_username,
        )

        dbs_info.append(db_info)
    return dbs_info


def init_default_database(db_info: DatabaseInfo) -> None:
    init_status = read_init_status()
    default_db_initialized = init_status["default_db_initialized"]
    if default_db_initialized:
        info("default database already initialized, skipping")
        return

    admin_username = db_info.owner

    # Bootstrap users and the primary database by connecting to the "init"
    # database which is built into the Docker image
    with connect(db_info.init_dsn) as conn:
        if not db_info.init_dsn:
            info("ERROR: init_dsn not found in spec, skipping default database initialization")
            sys.exit(1)
        with conn.cursor() as cur:
            cur.execute("SET log_statement = 'none';")
            stmts = [
                f"CREATE ROLE pgedge_superuser WITH NOLOGIN;",
                f"GRANT {get_superuser_roles()} TO pgedge_superuser WITH ADMIN true;",
                f"GRANT SET ON PARAMETER {SUPERUSER_PARAMETERS} TO pgedge_superuser;",
            ]
            for user in db_info.postgres_users.values():
                stmts.extend(create_user_statement(user))
            stmts += [
                f"CREATE DATABASE {db_info.database_name} OWNER {admin_username};",
                f"GRANT ALL PRIVILEGES ON DATABASE {db_info.database_name} TO {admin_username};",
                f"GRANT ALL PRIVILEGES ON DATABASE {db_info.init_dbname} TO {admin_username};",
                f"GRANT ALL PRIVILEGES ON DATABASE {db_info.database_name} TO pgedge;",
                f"ALTER USER pgedge WITH PASSWORD '{db_info.pgedge_pw}' LOGIN SUPERUSER REPLICATION;",
            ]
            for statement in stmts:
                cur.execute(statement)

    info("successfully bootstrapped database users")

    # Drop the init database and user
    with connect(db_info.internal_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET log_statement = 'none';")
            stmts = [
                f"DROP DATABASE {db_info.init_dbname};",
                f"DROP USER {db_info.init_username};",
            ]
            for statement in stmts:
                cur.execute(statement)

    info("successfully dropped init database")

    schemas = ["public", "spock", "pg_catalog", "information_schema"]

    init_spock_node(db_info, schemas)

    # Give the other nodes a couple seconds to reach this point as well. The
    # below code will retry but doing this means fewer errored attempts in the
    # log file. Other nodes should be able to connect to us at this point.
    time.sleep(5)

    # Wait for each peer to come online and then subscribe to it
    subscribed = init_peer_spock_subscriptions(db_info, True)
    if not subscribed:
        info(f"No need to subscribe to peers for {db_info.database_name}, skipping")
    else:
        info(f"default database initialized ({db_info.node_name})")
        update_default_db_init_status(True)

def init_database(db_info: DatabaseInfo) -> None:
    init_status = read_init_status()
    current_status = init_status["dbs_initialized"].get(db_info.database_name)
    if current_status is not None:
        info(f"database {db_info.database_name} already fully initialized, skipping")
        return

    # Check if database exists and create if it doesn't
    with connect(db_info.internal_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET log_statement = 'none';")
            # Check if database exists
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (db_info.database_name,)
            )
            db_exists = cur.fetchone() is not None
            
            if not db_exists:
                # Create the database if it doesn't exist
                cur.execute(f"CREATE DATABASE {db_info.database_name} OWNER {db_info.owner};")
                info(f"created database {db_info.database_name}")
            else:
                info(f"database {db_info.database_name} already exists")
            
            # Grant permissions regardless of whether we just created the database
            stmts = [
                f"GRANT ALL PRIVILEGES ON DATABASE {db_info.database_name} TO {db_info.owner};",
                f"GRANT ALL PRIVILEGES ON DATABASE {db_info.database_name} TO pgedge;",
            ]
            for statement in stmts:
                cur.execute(statement)
    
    update_database_init_status(db_info.database_name, DatabaseStatus.CREATED)
    info(f"successfully configured database {db_info.database_name}")


def init_peer_spock_subscriptions(db_info: DatabaseInfo, drop_existing: bool = False) -> bool:
    peers = [node for node in db_info.nodes if node["id"] != db_info.node_id]
    if len(peers) == 0:
        info(f"no peers found for database {db_info.database_name}, skipping peer spock subscriptions")
        return False
    with connect(db_info.local_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            for peer in peers:
                info("waiting for peer:", peer["name"])
                peer_dsn = dsn(
                    dbname=db_info.database_name,
                    user="pgedge",
                    host=get_hostname(peer),
                )
                sub_name = f"sub_{db_info.database_name}_{db_info.node_name}_{peer['name']}"
                wait_for_spock_node(peer_dsn)
                if drop_existing:
                    spock_sub_drop(cur, sub_name)
                time.sleep(2)
                spock_sub_create(
                    cur, sub_name, peer_dsn
                )
                info("subscribed to peer:", peer["name"])
            return True
    return False
def init_spock_node(db_info: DatabaseInfo, schemas: list[str]) -> None:

    with connect(db_info.spock_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET log_statement = 'none';")
            stmts = [
                f"CREATE EXTENSION IF NOT EXISTS spock;",
                f"CREATE EXTENSION IF NOT EXISTS snowflake;",
                f"CREATE EXTENSION IF NOT EXISTS pg_stat_statements;",
            ]
            if "pgcat_auth" in db_info.postgres_users:
                # supports auth_query from pgcat
                stmts.append(f"GRANT SELECT ON pg_shadow TO pgcat_auth;")
            for user in db_info.postgres_users.values():
                stmts.extend(
                    alter_user_statements(user, db_info.database_name, schemas)
                )
            stmts.append(
                f"SELECT spock.node_create(node_name := '{db_info.node_name}', dsn := '{db_info.spock_dsn}') WHERE '{db_info.node_name}' NOT IN (SELECT node_name FROM spock.node);"
            )
            for statement in stmts:
                cur.execute(statement)

    with connect(db_info.local_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET log_statement = 'none';")
            stmts = []
            for user in db_info.postgres_users.values():
                stmts.extend(
                    alter_user_statements(user, db_info.database_name, ["public"])
                )
            for statement in stmts:
                cur.execute(statement)

    with connect(db_info.spock_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET log_statement = 'none';")
            stmts = []
            for user in db_info.postgres_users.values():
                stmts.extend(
                    alter_user_statements(user, db_info.database_name, schemas)
                )
            for statement in stmts:
                cur.execute(statement)


def main() -> None:
    # The spec contains the desired settings
    try:
        spec = read_config()
    except FileNotFoundError:
        info("ERROR: spec not found, skipping initialization")
        sys.exit(1)

    # Parse the spec so we can pass it around
    default_db_info = get_default_db_info(spec)

    if default_db_info.mode == "offline":
        info("mode offline configured, postgres will not start")
        while True:
            time.sleep(1)
    else:
        # Give Postgres a moment to start
        time.sleep(3)

        initialized = not can_connect(default_db_info.init_dsn) and can_connect(default_db_info.local_dsn)

        if initialized:
            info("database node already initialized, skipping initialization.")
        else:
            info("initializing database node...")
            init_default_database(default_db_info)
    
    # Initialize the other databases
    dbs_info = get_dbs_info(spec)
    for db_info in dbs_info:
        init_database(db_info)
    
    # Init spock node for all databases
    schemas = ["public", "spock", "pg_catalog", "information_schema"]
    for db_info in dbs_info:
        init_status = read_init_status()
        current_status = init_status["dbs_initialized"].get(db_info.database_name)
        if current_status is None: 
            info(f"database {db_info.database_name} not initialized, something went wrong! Please restart the node.")
            sys.exit(1)
        elif current_status == DatabaseStatus.CREATED:
            info(f"database {db_info.database_name} created, initializing spock node")
            init_spock_node(db_info, schemas)
            update_database_init_status(db_info.database_name, DatabaseStatus.INITED)
            time.sleep(5)
            info(f"spock node of {db_info.database_name} initialized.")
        else:
            info(f"database spock node {db_info.database_name} already initialized, skipping")

    # Init peer subscriptions for all databases
    for db_info in dbs_info:
        init_status = read_init_status()
        current_status = init_status["dbs_initialized"].get(db_info.database_name)
        if current_status == DatabaseStatus.SUBSCRIBED:
            info(f"database {db_info.database_name} already subscribed to peers, skipping")
        else:
            info(f"database {db_info.database_name} not subscribed to peers, subscribing")
            subscribed = init_peer_spock_subscriptions(db_info, True)
            if subscribed:
                update_database_init_status(db_info.database_name, DatabaseStatus.SUBSCRIBED)
                info(f"database {db_info.database_name} subscribed to peers")
            else:
                info(f"No need to subscribe to peers for {db_info.database_name}, skipping")

    info(f"cluster node initialized ({db_info.node_name})")


if __name__ == "__main__":
    main()
