import configparser

CURRENT_SCHEMA = None
DB_CONFIG = None


class Config:
    global CURRENT_SCHEMA
    global DB_CONFIG

    # Load configuration from file
    config = configparser.ConfigParser()
    config.read("config.ini")

    # Build database connection string
    DB_CONFIG = config["database"]
    SQLALCHEMY_DATABASE_URI = (
        f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Get schema name
    CURRENT_SCHEMA = config["schema"]["name"]
