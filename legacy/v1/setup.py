import os
import shlex
import platform
import subprocess
import sys
from getpass import getpass
import configparser
import json


def check_os():
    """Detect operating system and return 'windows' or 'linux'"""
    return 'windows' if platform.system().lower() == 'windows' else 'linux'


def elevate_permissions():
    """Request elevated permissions if not root/admin"""
    if (os.name != 'posix' or os.geteuid() != 0) and check_os() == 'linux':
        print("Requires sudo privileges")
        subprocess.call(['sudo', 'python3'] + sys.argv)
        sys.exit()
    elif check_os() == 'windows' and not ctypes.windll.shell32.IsUserAnAdmin():
        print("Requires administrator privileges")
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                            " ".join(sys.argv), None, 1)
        sys.exit()


def run_sql_script(script_path, **kwargs):
    """Execute PostgreSQL script using psql command"""
    cmd = [
        'psql', '-U',
        kwargs.get('user', 'postgres'), '-h',
        kwargs.get('host', 'localhost'), '-p',
        kwargs.get('port', '5432'), '-d', 'todo_grocery', '-f', script_path
    ]
    printable_cmd = ' '.join(shlex.quote(arg) for arg in cmd)
    print(f"\nExecuting command:\n{printable_cmd}\n")

    subprocess.run(cmd, check=True)


def create_database():
    """Execute database creation script"""
    run_sql_script('ddl/1-create_database.sql')


def create_roles():
    """Create roles with password management"""
    envs = ['dev', 'test', 'prod']
    for env in envs:
        resp = input(
            f"Do you want to create a role {env}_todo_grocery for {env.upper()} environment? Y/N: "
        ).lower()
        if resp == 'y':
            script = f'ddl/2_{envs.index(env)+1}-create_role_{env}.sql'
            password = getpass(
                f"Enter password for {env} role (default: password): "
            ) or 'password'
            run_sql_script(script, password=password)


def create_schemas():
    """Create environment-specific database structures"""
    envs = ['dev', 'test', 'prod']
    for env in envs:
        resp = input(
            f"Do you want to define database for {env.upper()} environment? Y/N: "
        ).lower()
        if resp == 'y':
            run_sql_script(f'ddl/3_{envs.index(env)+1}-ddl_{env}.sql')


def create_venv():
    """Create Python virtual environment"""
    venv_name = 'easytodo'
    subprocess.run([sys.executable, '-m', 'venv', venv_name], check=True)


def install_requirements():
    """Install Python dependencies"""
    pip = 'easytodo/Scripts/pip' if check_os(
    ) == 'windows' else 'easytodo/bin/pip'
    subprocess.run([pip, 'install', '-r', 'requirements.txt'], check=True)


def manage_firewall():
    """Configure firewall rules for application port"""
    port = input("Enter port number: ")

    if check_os() == 'linux':
        action = input("Choose action (ACCEPT/REJECT/DROP): ").upper()
    else:
        action = input("Choose action (allow/deny): ").lower()

    if check_os() == 'linux':
        cmd_remove_accept = [
            'sudo', 'iptables', '-D', 'INPUT', '-p', 'tcp', '--dport', port,
            '-j', 'ACCEPT'
        ]

        cmd_remove_reject = [
            'sudo', 'iptables', '-D', 'INPUT', '-p', 'tcp', '--dport', port,
            '-j', 'REJECT'
        ]

        cmd_remove_drop = [
            'sudo', 'iptables', '-D', 'INPUT', '-p', 'tcp', '--dport', port,
            '-j', 'DROP'
        ]

        cmd_add_rule = [
            'sudo', 'iptables', '-A', 'INPUT', '-p', 'tcp', '--dport', port,
            '-j', action
        ]

        cmd_save_rule = ['sudo', 'iptables-save']

        #First, remove existing rules to avoid collisions
        subprocess.run(cmd_remove_accept)
        subprocess.run(cmd_remove_reject)
        subprocess.run(cmd_remove_drop)
        subprocess.run(cmd_add_rule, check=True)
        subprocess.run(cmd_save_rule, check=True)

        printable_cmd_remove_accept = ' '.join(
            shlex.quote(arg) for arg in cmd_remove_accept)
        print(f"\nExecuted command:\n{printable_cmd_remove_accept}\n")

        printable_cmd_remove_reject = ' '.join(
            shlex.quote(arg) for arg in cmd_remove_reject)
        print(f"\nExecuted command:\n{printable_cmd_remove_reject}\n")

        printable_cmd_remove_drop = ' '.join(
            shlex.quote(arg) for arg in cmd_remove_drop)
        print(f"\nExecuted command:\n{printable_cmd_remove_drop}\n")

        printable_cmd_add_rule = ' '.join(
            shlex.quote(arg) for arg in cmd_add_rule)
        print(f"\nExecuted command:\n{printable_cmd_add_rule}\n")

        printable_cmd_save_rule = ' '.join(
            shlex.quote(arg) for arg in cmd_save_rule)
        print(f"\nExecuted command:\n{printable_cmd_save_rule}\n")

    else:
        direction = 'Inbound'
        rule_action = 'Allow' if action == 'allow' else 'Block'

        remove_rules = [
            'Remove-NetFirewallRule', '-Direction', direction, '-Protocol',
            'TCP', '-LocalPort', port, '-ErrorAction', 'SilentlyContinue'
        ]

        add_rule = [
            'New-NetFirewallRule', '-DisplayName', f'Custom Rule {port} TCP',
            '-Direction', direction, '-Protocol', 'TCP', '-LocalPort', port,
            '-Action', rule_action, '-Profile', 'Any'
        ]

        # Remove existing rules for port (Allow i Block)
        subprocess.run(['powershell', '-Command'] + remove_rules, check=False)

        # Add new rule
        subprocess.run(['powershell', '-Command'] + add_rule, check=True)

        printable_cmd_remove_rules = ' '.join(
            shlex.quote(arg) for arg in remove_rules)
        print(f"\nExecuted command:\n{printable_cmd_remove_rules}\n")

        printable_cmd_add_rule = ' '.join(shlex.quote(arg) for arg in add_rule)
        print(f"\nExecuted command:\n{printable_cmd_add_rule}\n")


def get_input(prompt, default):
    """Universal input handler with default values"""
    response = input(f"{prompt} [{default}]: ").strip()
    return response if response else default


def generate_application_configuration():
    """Main configuration generator workflow"""
    print("\nApplication configuration\n" + "=" * 40)

    # Collect configuration parameters
    db_host = get_input("Database host", "localhost")
    db_port = get_input("Database port", "5432")
    db_user = get_input("Database user", "test_todo_grocery")
    db_password = get_input("Database password", "password")
    db_name = get_input("Database name", "todo_grocery")

    schema_name = get_input("Schema name", "test")

    print(
        "\nTo keep websocket working correctly use app server address for host name instead of localhost or 0.0.0.0"
    )
    app_host = get_input("Application backend host", "192.168.0.2")
    app_port = get_input("Application port", "5000")

    # Generate config.ini
    config = configparser.ConfigParser()
    config['database'] = {
        'host': db_host,
        'port': db_port,
        'user': db_user,
        'password': db_password,
        'dbname': db_name
    }
    config['schema'] = {'name': schema_name}
    config['host'] = {'name': app_host, 'port': app_port}

    with open('config.ini', 'w') as f:
        config.write(f)

    # Generate socketio.json
    socket_config = {"host": {"name": app_host, "port": app_port}}

    os.makedirs('app/static/js', exist_ok=True)
    with open('app/static/js/socketio.json', 'w') as f:
        json.dump(socket_config, f, indent=4)

    print("\nCreated configuration files:")
    print(f"- {os.path.abspath('config.ini')}")
    print(f"- {os.path.abspath('app/static/js/socketio.json')}")


def generate_config():
    """Generate application configuration files"""
    generate_application_configuration()


def initial_setup():
    """Complete environment setup workflow"""
    pg_creds = {}
    if input("Requires PostgreSQL admin login? (Y/N): ").lower() == 'y':
        pg_creds['user'] = input("Admin username: ")
        pg_creds['password'] = getpass("Admin password: ")

    for step in [
            create_database, create_roles, create_schemas, create_venv,
            install_requirements, manage_firewall, generate_config
    ]:
        step()


def main_menu():
    """Display and handle main menu options"""
    menu = {
        '0': initial_setup,
        '1': create_database,
        '2': create_roles,
        '3': create_schemas,
        '4': create_venv,
        '5': install_requirements,
        '6': manage_firewall,
        '7': generate_config
    }

    while True:
        print("\nMain Menu:")
        for k, v in menu.items():
            print(f"{k} - {v.__doc__}")
        choice = input("\nSelect option (q to quit): ").strip()

        if choice.lower() == 'q':
            break
        if choice in menu:
            try:
                menu[choice]()
            except Exception as e:
                print(f"Error: {str(e)}")
        else:
            print("Invalid option")


if __name__ == "__main__":
    elevate_permissions()
    main_menu()
