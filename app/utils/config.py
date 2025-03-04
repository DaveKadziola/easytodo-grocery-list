import configparser

CURRENT_SCHEMA = None
DB_CONFIG = None
APP_HOST_NAME = None
APP_PORT = None


class Config:
    global CURRENT_SCHEMA
    global DB_CONFIG
    global APP_HOST_NAME
    global APP_PORT

    # Load configuration from file
    config = configparser.ConfigParser()
    config.read("config.ini")

    # Build database connection string
    DB_CONFIG = config["database"]
    SQLALCHEMY_DATABASE_URI = (
        f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Get schema name
    CURRENT_SCHEMA = config["schema"]["name"]

    # Get host and port
    APP_HOST_NAME = config["host"]["name"]
    APP_PORT = config["host"]["port"]
