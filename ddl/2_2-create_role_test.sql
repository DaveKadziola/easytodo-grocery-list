\connect todo_grocery;
-- DROP ROLE test_todo_grocery;

CREATE ROLE test_todo_grocery WITH
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    INHERIT
	LOGIN
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT -1;