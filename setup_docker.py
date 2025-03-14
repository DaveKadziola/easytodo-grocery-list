import os
import subprocess
import re
import time


def check_placeholders(file_path):
    """Check if any placeholders (<...>) remain in .env file"""
    try:
        with open(file_path, 'r') as f:
            content = f.read()
            if re.search(r'<.*?>', content):
                print(
                    f"\nERROR! Please replace placeholders with target values in {file_path}:"
                )
                print(content, "\n")
                return True
        return False
    except FileNotFoundError:
        print(f"Error: File {file_path} not found")
        return True


def get_env_variables(file_path):
    """Read environment variables from .env file"""
    env_vars = {}
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
        return env_vars
    except FileNotFoundError:
        print(f"Error: File {file_path} not found")
        return {}


def run_docker_compose(compose_file, env_file):
    """Run containers and verify their status"""
    if check_placeholders(env_file):
        print("Error: Unconfigured environment variables!")
        return False

    try:
        # Run containers in detached mode
        subprocess.run(f"docker compose -f {compose_file} up -d",
                       shell=True,
                       check=True,
                       capture_output=True)

        # Check status after 5 seconds
        time.sleep(5)
        result = subprocess.run(f"docker compose -f {compose_file} ps",
                                shell=True,
                                capture_output=True,
                                text=True)

        # Verify container status
        if "up" not in result.stdout.lower():
            print("Container not running properly")
            return False

        # Display connection information with clickable links
        env_vars = get_env_variables(env_file)

        app_host = env_vars.get('APP_HOST_NAME')
        app_port = env_vars.get('APP_HOST_PORT')
        app_url = f"http://{app_host}:{app_port}"
        clickable_app_link = f"\x1b]8;;{app_url}\x1b\\{app_host}:{app_port}\x1b]8;;\x1b\\"

        db_host = env_vars.get('DB_HOST')
        db_port = env_vars.get('DB_PORT')
        db_url = f"mysql://{db_host}:{db_port}"
        clickable_db_link = f"\x1b]8;;{db_url}\x1b\\{db_host}:{db_port}\x1b]8;;\x1b\\"

        print(
            f"\nApplication has started and is available at {clickable_app_link}"
        )
        print(f"Database can be accessed at {clickable_db_link}\n")

        return True

    except subprocess.CalledProcessError as e:
        print(f"Execution error: {e.stderr}")
        return False


def main():
    while True:
        print("Docker container compositor")
        print("1 - Setup Docker compose using external database")
        print("2 - Setup Docker compose using integrated database")
        print("3 - Exit")

        choice = input("Enter your choice (1-3): ")

        if choice == '1':
            compose_path = "docker/v1.0.0/no-db/docker-compose.yml"
            env_path = "docker/v1.0.0/no-db/.env"
            run_docker_compose(compose_path, env_path)

        elif choice == '2':
            compose_path = "docker/v1.0.0/with-db/docker-compose.yml"
            env_path = "docker/v1.0.0/with-db/.env"
            run_docker_compose(compose_path, env_path)

        elif choice == '3':
            print("Exiting...")
            break

        else:
            print("Invalid choice. Please try again.")


if __name__ == "__main__":
    main()
