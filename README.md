# Easy ToDo &amp; Grocery List 

This is a simple web-based application for managing tasks and categories. The application uses Flask with SQLAlchemy as the backend framework, PostgreSQL as the database, and Bootstrap for styling.

---

## Prerequisites

Before starting, ensure you have the following installed on your system:

1. **Python 3.8 or Higher**
   - To check your Python version:
     ```
     python --version
     ```
   - If Python is not installed, download it from [python.org](https://www.python.org/downloads/).

2. **PostgreSQL Database**
   - Ensure PostgreSQL is installed and running.
   - To verify:
     ```
     psql --version
     ```
   - If not installed, download PostgreSQL from [postgresql.org](https://www.postgresql.org/download/).

3. **Virtual Environment Tool**
   - Use Python's built-in `venv` module or install `virtualenv`:
     ```
     pip install virtualenv
     ```

4. **Git (Optional)**
   - If cloning from a repository, ensure Git is installed:
     ```
     git --version
     ```
   - Download Git from [git-scm.com](https://git-scm.com/) if necessary.

5. **Text Editor or IDE**
   - Use an editor like Visual Studio Code, PyCharm, or any text editor of your choice.

---

## Setting Up the Python Environment

### Step 1: Clone the Repository
Clone the repository to your local machine:
```
git clone git@github.com:DaveKadziola/easytodo-grocery-list.git
```


### Step 2: Create a Virtual Environment
Create and activate a virtual environment:

For Linux/MacOS:
```
python3 -m venv easytodo
source easytodo/bin/activate
```

For Windows:
```
python -m venv easytodo
easytodo\Scripts\activate
```

### Step 3: Install Dependencies
Install required Python packages using `pip`:
```
pip install -r requirements.txt
```

---

## Configuring the Application

### Step 1: Create `config.ini`
Create a `config.ini` file in the root directory of the project. This file stores sensitive information like database credentials and schema names.

#### Example `config.ini` File:
```
[database]
host = localhost
port = 5432
user = your_database_user
password = your_database_password
dbname = todo_grocery

[schema]
name = prod
```

#### Explanation of Fields:
- **host**: The address of your PostgreSQL server (e.g., `localhost` for local development).
- **port**: The port used by PostgreSQL (default is `5432`).
- **user**: Your PostgreSQL username.
- **password**: Your PostgreSQL password.
- **dbname**: The name of the database to use.
- **name**: The schema name for your database (`prod` and `dev` in repo's ddl scripts).

### Step 2: Add `config.ini` to `.gitignore`
Ensure that sensitive information in `config.ini` is not committed to version control. Add the following line to `.gitignore`:
```
config.ini
easytodo\
```

---

## Initializing the Database

### Step 1: Create the Database and Schema
Log in to PostgreSQL and create the database, roles, tables, grants and etc by running scripts from ddl catalog in the following order:
```
1-create_database.sql
2-create_roles.sql
3-ddl_dev.sql
4-ddl_prod.sql
```

---

## Running the Application

Start the application:
```
python3 app.py
```

The application should now be accessible at `http://127.0.0.1:8100`.

___

## Additional settings

If you want to have access from other devices in local network, allow port in firewall as follows:
```
# for UFW
sudo ufw allow 8100/tcp

# for iptables
sudo iptables -A INPUT -p tcp --dport 8100 -j ACCEPT 
```

If you want to restart the application but port is in use, check for PID of process listening at port 8100 and kill process by its PID
```
sudo lsof -i :8100
sudo kill -9 PID
```
