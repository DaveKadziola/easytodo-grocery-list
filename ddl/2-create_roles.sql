-- DROP ROLE prod_todo_grocery;

CREATE ROLE prod_todo_grocery WITH
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    INHERIT
    NOLOGIN
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT -1;

-- Permissions
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.categories TO prod_todo_grocery;
GRANT SELECT, USAGE, UPDATE ON SEQUENCE prod.categories_id_seq TO prod_todo_grocery;
GRANT USAGE ON SCHEMA prod TO prod_todo_grocery;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.task_assignment TO prod_todo_grocery;
GRANT SELECT, USAGE, UPDATE ON SEQUENCE prod.task_assignment_id_seq TO prod_todo_grocery;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.tasks TO prod_todo_grocery;
GRANT SELECT, USAGE, UPDATE ON SEQUENCE prod.tasks_id_seq TO prod_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA prod GRANT DELETE, SELECT, INSERT, UPDATE ON TABLES TO prod_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA prod GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO prod_todo_grocery;

-- DROP ROLE dev_todo_grocery;
CREATE ROLE dev_todo_grocery WITH
	NOSUPERUSER
	NOCREATEDB
	NOCREATEROLE
	INHERIT
	NOLOGIN
	NOREPLICATION
	NOBYPASSRLS
	CONNECTION LIMIT -1;

-- Permissions
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE dev.categories TO dev_todo_grocery;
GRANT SELECT, USAGE, UPDATE ON SEQUENCE dev.categories_id_seq TO dev_todo_grocery;
GRANT USAGE ON SCHEMA dev TO dev_todo_grocery;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE dev.task_assignment TO dev_todo_grocery;
GRANT SELECT, USAGE, UPDATE ON SEQUENCE dev.task_assignment_id_seq TO dev_todo_grocery;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE dev.tasks TO dev_todo_grocery;
GRANT SELECT, USAGE, UPDATE ON SEQUENCE dev.tasks_id_seq TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT DELETE, SELECT, INSERT, UPDATE ON TABLES TO dev_todo_grocery;
