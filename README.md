# EasyTodo Grocery List

EasyTodo Grocery List is a simple application designed to manage your grocery list and tasks. The application features an intuitive user interface with seamless operations—updates and actions occur without reloading the page thanks to real-time data synchronization (including automatic state updates after reconnecting). It is optimized for both mobile and desktop platforms and is intended for self-hosted use only, using configuration script or Docker containers.

## Key Features

- **Task and Category Management:** Easily add, edit, delete, and reorganize tasks and categories.
- **Real-Time Synchronization:** Keeps the application state current even after temporary disconnections.
- **Seamless User Experience:** All updates occur dynamically without reloading the page.
- **Responsive Design:** Optimized for mobile devices and desktops.

## Technologies Used

- **Python** – Primary programming language.
- **Flask** – Web framework powering the backend services.
- **Flask-SocketIO** – Enables real-time data synchronization and notifications.
- **SQLAlchemy** – ORM for database management.
- **psycopg2** – Listens to the PostgreSQL database and triggers real-time synchronization via database triggers and functions.
- **PostgreSQL** – Relational database used for data storage.
- **HTML/CSS/JavaScript** – Front-end technologies for building the user interface.

## API Endpoints and Functionalities

The application exposes several API endpoints for managing categories, tasks, and task assignments:

### Categories

- **POST `/v1/add_category`**  
  Adds a new category.

- **PUT `/v1/rename_category/<category_id>`**  
  Renames an existing category.

- **DELETE `/v1/delete_category/<category_id>`**  
  Deletes a category along with its associated tasks.

- **PUT `/v1/move_category`**  
  Moves a category up or down in the list.

- **GET `/v1/get_all_categories/`**  
  Retrieves a list of all categories.

### Index

- **GET `/`**  
  Renders the main page displaying categories and their assigned tasks.

### Task Assignments

- **GET `/v1/get_tasks_by_category/<category_id>`**  
  Retrieves tasks assigned to a specific category.

### Tasks

- **POST `/v1/add_task/`**  
  Adds a new task and assigns it to a category.

- **PUT `/v1/update_task/<task_id>`**  
  Updates the details of an existing task.

- **DELETE `/v1/delete_task/<task_id>`**  
  Deletes a task.

- **PUT `/v1/move_task`**  
  Moves a task from one category to another.

- **PUT `/v1/toggle_task/<task_id>`**  
  Toggles a task's status between completed and incomplete.

For more details on the API endpoints, refer to the Flasgger documentation at `http://hostname:port/apidocs`.

## Database Structure

- **Schema:**  
  The DDL scripts provide database for schema `dev`, `test`, `prod` and is owned by PostgreSQL.

- **Tables and Sequences:**

  - **categories:** Stores unique task categories with an auto-incrementing ID, name, timestamps, and position.
  - **tasks:** Contains task records with an auto-incrementing ID, name, description, timestamps, and a completion status.
  - **temporary_data:** Temporarily holds data for operations like deletion or movement of categories/tasks.
  - **updates_log:** Logs update events with a JSONB payload for real-time synchronization and notification.
  - **task_assignment:** Links tasks to categories with an auto-incrementing ID, and records the assignment timestamp.
  - Sequences are defined for each table to auto-increment the primary keys.

- **Triggers & Functions:**  
  Database triggers call functions (e.g., `handle_category`, `handle_task`, etc.) to log events and notify the application of changes via PostgreSQL's notification system.

## Configuration & Launch

### No container setup

- **Configuration:**  
  The application requires PostgreSQL database instance. If you don't have it installed yet, you must setup it first. The application is configured using the `setup.py` script, which performs the following tasks:

  - **OS Detection & Permissions:** Determines the operating system and requests elevated permissions if necessary.
  - **Database Setup:** Executes SQL scripts to create the database, roles, and schemas.
  - **Virtual Environment:** Creates a Python virtual environment for the application.
  - **Dependency Installation:** Installs required Python packages from `requirements.txt`.
  - **Firewall Configuration:** Configures firewall rules for the specified application port.
  - **Configuration File Generation:** Generates configuration files (`config.ini` and `app/static/js/socketio.json`) with user-specified parameters.
  - **Interactive Menu:** Provides a menu-driven interface to perform the initial setup and configuration tasks.

  Run the script as follows:

  ```bash
  python3 setup.py
  ```

- **Launch:**  
  Start the main application by executing the `run.py` script:
  ```bash
  source easytodo/bin/activate
  python3 run.py
  ```

### Container setup

In the `docker` directory you can find two variants like `no-db` which only builds the Docker container for application and similarly like in no container configuration, it requires to have installed PostgreSQL instance. The `with-db` version does have integrated PostgreSQL service.

To build the Docker containers you must replace all placeholders for environment variables between <> characters in the `docker/v1.0.0/no-db/.env` and `docker/v1.0.0/with-db/.env`. In the in `docker/v1.0.0/no-db/.env` if it's needed you may need to adjust rest parameters like database name, schema, user and etc.

By default, both containers work in the host mode so on both the application can be accessed in whole local network from any device. Both containers can work simultaneously withouts any conflicts.

Build and start of Docker containers is very easy. Just run the following script:

```bash
python3 setup_docker.py
```

Then select desired cointainer variant you wish to setup.

If you forgot replace placeholders in `.env` files, don't worry. Script takes care of that and will you remind about it.

If you will face any issues with containerized application you can execute the docker compose command directly instead of the `setup_docker.py` script.
For the version with no integrated database:

```bash
docker compose -f docker/v1.0.0/no-db/docker-compose.yml up --build
```

For the version with integrated database:

```bash
docker compose -f docker/v1.0.0/with-db/docker-compose.yml up --build
```

Alternatively, you can add the `--force-recreate` parameter in the end.

In case of using the no-db version, make sure your PostgreSQL config allows connections outside localhost.
In the `postgresql.conf` check if `listen_addresses` is uncommented and doesn't limit to localhost. You can set this line up like follows:

```bash
listen_addresses = '*'
```

In the `pg_hba.conf` in the section `# IPv4 local connections` allow connections from whole local network (or instead of `0.0.0.0/0` limit to specific subnets like `172.10.0.0/24` - up to you and your requirements):

```bash
# IPv4 local connections:
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             0.0.0.0/0               scram-sha-256
```

... and restart the PostgreSQL service:

```bash
sudo systemctl restart postgresql
```

On Windows execute the following commands (replace `<version>` with number of your installed version):

```bash
net stop postgresql-x64-<version>
net start postgresql-x64-<version>
```

If you still will have issues, check you firewall and port rules (use `setup.py` to manage rules or do it manually by yourself).

Enjoy! :)

## Further Improvements & Development Ideas

- [ ] Optimize psycopg2 connections (create connection pools) and consider replacing SQLAlchemy with psycopg2 to unify database operations and reduce library imports.
- [ ] Add a pg-cron job to delete old records and define criteria for what constitutes an "old" record.
- [ ] Consider whether synchronization with a NoSQL database using Redis makes sense if workspaces are introduced and the application is adapted for simultaneous use by multiple users.
- [ ] Add a highlight effect to the category where a task is dropped.
- [ ] Disable task highlighting during state updates (when processing data from `request_update`).
- [ ] Implement drag-and-drop functionality for both mouse and touch devices using [interactjs.io](https://interactjs.io) (currently, only basic mouse dragging is supported).
- [ ] Introduce a login panel—potentially a simple login by email with a code.
- [ ] Enable the creation of workspaces with features to assign, invite, and share with other users, allowing multiple users to collaborate within a workspace.
- [ ] For workspaces, add sections such as "Your Workspaces" and "Assigned to Workspace".
- [ ] Add multilanguage support.
- [ ] Analyze of Dockerfiles if output images can be smaller (especially postgres one)
- [ ] Improvements of network configuration in Dockerfiles - to be verified after deployment on homelab and composing with other services running in the same self-hosted ecosystem
