import configparser
import json
import os

def get_input(prompt, default):
    response = input(f"{prompt} [{default}]: ").strip()
    return response if response else default

def main():
    print("Application configuration\n" + "="*40)

    # Collect user input
    db_host = get_input("Database host", "localhost")
    db_port = get_input("Database port", "5432")
    db_user = get_input("Database user", "postgres")
    db_password = get_input("Database password", "password")
    db_name = get_input("Database name", "todo_grocery")

    schema_name = get_input("Schema name", "prod")

    print("To keep websocket working correctly use app server address for host name instead of localhost or 0.0.0.0")
    app_host = get_input("Application backend host", "192.168.0.2")
    app_port = get_input("Application port", "8100")

    # Create config.ini
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

    # Create socketio.json
    socket_config = {
        "host": {
            "name": app_host,
            "port": app_port
        }
    }

    os.makedirs('app/static/js', exist_ok=True)
    with open('app/static/js/socketio.json', 'w') as f:
        json.dump(socket_config, f, indent=4)

    print("\nConfig files successfully created!")

if __name__ == "__main__":
    main()
